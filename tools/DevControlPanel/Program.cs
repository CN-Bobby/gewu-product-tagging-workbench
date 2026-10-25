using System.Threading;

namespace GeWuDevControl;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        using var mutex = new Mutex(true, @"Local\GeWuDevControlPanel", out var firstInstance);
        if (!firstInstance)
        {
            try { using var existing = EventWaitHandle.OpenExisting(@"Local\GeWuDevControlPanel.Show"); existing.Set(); }
            catch { MessageBox.Show("开发控制台已在运行，请查看任务栏通知区域。", "格物开发控制台"); }
            return;
        }
        using var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\GeWuDevControlPanel.Show");
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using var form = new MainForm(new RunCenterClient());
        var registration = ThreadPool.RegisterWaitForSingleObject(showEvent,
            (_, _) => { if (!form.IsDisposed && form.IsHandleCreated) form.BeginInvoke(form.RestoreFromTray); },
            null, Timeout.Infinite, false);
        try { Application.Run(form); }
        finally { registration.Unregister(null); }
    }
}
