using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Session;

namespace WinResMonNetAgent;

/// <summary>
/// 出力（1 行 1 メッセージの JSON）:
///   {"type":"ready","traffic":true,"dns":true,"error":null}
///   {"type":"traffic","rows":[["T",pid,"localIp",localPort,"remoteIp",remotePort,sentDelta,recvDelta], ...]}
///   {"type":"dns","items":[["ip","name"], ...]}
///   {"type":"error","message":"..."}
/// traffic の値は前回出力からの増分。標準入力が閉じられたら（親プロセスが終了したら）終了する。
/// </summary>
internal static class Program
{
    private const string KernelSessionName = "WinResMon-NetKernel";
    private const string DnsSessionName = "WinResMon-NetDns";
    private static readonly Guid DnsClientProvider = new("1C95126E-7EEA-49A9-A3FE-A378B03DDB4D");

    private sealed class Counter { public long Sent, Recv; }

    private static readonly ConcurrentDictionary<(char, int, string, int, string, int), Counter> Traffic = new();
    private static readonly ConcurrentQueue<(string Ip, string Name)> NewDns = new();
    private static readonly object OutLock = new();
    private static readonly Stream Stdout = Console.OpenStandardOutput();

    private static int Main(string[] args)
    {
        if (TraceEventSession.IsElevated() != true)
        {
            Write(new { type = "error", message = "管理者権限が必要です" });
            return 2;
        }

        TraceEventSession? kernel = null, dns = null;
        string? error = null;
        try { kernel = StartKernel(); }
        catch (Exception ex) { error = "kernel: " + ex.Message; }
        try { dns = StartDns(); }
        catch (Exception ex) { error = (error is null ? "" : error + " / ") + "dns: " + ex.Message; }

        Write(new { type = "ready", traffic = kernel is not null, dns = dns is not null, error });
        if (kernel is null && dns is null) return 3;

        // 親プロセスが標準入力を閉じたら（または親が終了したら）抜ける
        var stop = new ManualResetEventSlim();
        new Thread(() =>
        {
            try { while (Console.In.ReadLine() is not null) { } } catch { }
            stop.Set();
        }) { IsBackground = true }.Start();

        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Set(); };

        while (!stop.Wait(1000))
        {
            try { Flush(); }
            catch (IOException) { break; } // 出力先が閉じられた
        }

        try { kernel?.Dispose(); } catch { }
        try { dns?.Dispose(); } catch { }
        return 0;
    }

    private static TraceEventSession StartKernel()
    {
        TraceEventSession s;
        try
        {
            s = new TraceEventSession(KernelSessionName) { StopOnDispose = true };
            s.EnableKernelProvider(KernelTraceEventParser.Keywords.NetworkTCPIP);
        }
        catch
        {
            // 古い Windows では "NT Kernel Logger" でしかカーネルイベントを取れない
            s = new TraceEventSession(KernelTraceEventParser.KernelSessionName) { StopOnDispose = true };
            s.EnableKernelProvider(KernelTraceEventParser.Keywords.NetworkTCPIP);
        }

        // カーネルの TcpIp/UdpIp イベントは送受信どちらでも saddr/sport がローカル側、daddr/dport がリモート側
        var k = s.Source.Kernel;
        k.TcpIpSend += e => Add('T', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, true);
        k.TcpIpRecv += e => Add('T', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, false);
        k.TcpIpSendIPV6 += e => Add('T', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, true);
        k.TcpIpRecvIPV6 += e => Add('T', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, false);
        k.UdpIpSend += e => Add('U', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, true);
        k.UdpIpRecv += e => Add('U', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, false);
        k.UdpIpSendIPV6 += e => Add('U', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, true);
        k.UdpIpRecvIPV6 += e => Add('U', e.ProcessID, e.saddr, e.sport, e.daddr, e.dport, e.size, false);

        Run(s);
        return s;
    }

    private static TraceEventSession StartDns()
    {
        var s = new TraceEventSession(DnsSessionName) { StopOnDispose = true };
        s.EnableProvider(DnsClientProvider, TraceEventLevel.Informational, ulong.MaxValue);
        s.Source.Dynamic.All += OnDns;
        Run(s);
        return s;
    }

    private static void Run(TraceEventSession s) =>
        new Thread(() => { try { s.Source.Process(); } catch { } }) { IsBackground = true }.Start();

    private static void Add(char proto, int pid, IPAddress local, int lport, IPAddress remote, int rport, int size, bool send)
    {
        if (size <= 0) return;
        var key = (proto, pid, Normalize(local), lport, Normalize(remote), rport);
        var c = Traffic.GetOrAdd(key, _ => new Counter());
        if (send) Interlocked.Add(ref c.Sent, size); else Interlocked.Add(ref c.Recv, size);
    }

    // Event 3008: クエリ完了。QueryResults は "type:  5 alias.example.com;::ffff:93.184.216.34;" のような形式
    private static void OnDns(TraceEvent e)
    {
        if (e.ProviderGuid != DnsClientProvider || (int)e.ID != 3008) return;
        try
        {
            var name = e.PayloadByName("QueryName") as string;
            var results = e.PayloadByName("QueryResults") as string;
            if (string.IsNullOrWhiteSpace(name) || string.IsNullOrWhiteSpace(results)) return;
            foreach (var raw in results.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            {
                if (raw.StartsWith("type:", StringComparison.OrdinalIgnoreCase)) continue;
                if (IPAddress.TryParse(raw, out var ip)) NewDns.Enqueue((Normalize(ip), name.TrimEnd('.')));
            }
        }
        catch { }
    }

    private static void Flush()
    {
        var rows = new List<object[]>();
        foreach (var (k, c) in Traffic)
        {
            long s = Interlocked.Exchange(ref c.Sent, 0);
            long r = Interlocked.Exchange(ref c.Recv, 0);
            if (s == 0 && r == 0) { Traffic.TryRemove(k, out _); continue; } // 1 秒間通信がなければ捨てる
            rows.Add(new object[] { k.Item1.ToString(), k.Item2, k.Item3, k.Item4, k.Item5, k.Item6, s, r });
        }
        if (rows.Count > 0) Write(new { type = "traffic", rows });

        var items = new List<string[]>();
        while (NewDns.TryDequeue(out var d)) items.Add(new[] { d.Ip, d.Name });
        if (items.Count > 0) Write(new { type = "dns", items });
    }

    private static string Normalize(IPAddress a)
    {
        if (a.AddressFamily == AddressFamily.InterNetworkV6 && a.IsIPv4MappedToIPv6) a = a.MapToIPv4();
        if (a.AddressFamily == AddressFamily.InterNetworkV6) a.ScopeId = 0;
        return a.ToString();
    }

    private static void Write(object msg)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(msg);
        lock (OutLock)
        {
            Stdout.Write(bytes);
            Stdout.WriteByte((byte)'\n');
            Stdout.Flush();
        }
    }
}
