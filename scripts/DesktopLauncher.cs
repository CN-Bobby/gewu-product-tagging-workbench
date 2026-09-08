using System;
using System.IO;
using System.Net;
using System.Diagnostics;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using System.Threading;

class DesktopLauncher {
 [STAThread] static int Main(string[] args) {
  try { return Run(args); } catch(Exception e) { if(Array.IndexOf(args,"--no-open")<0) MessageBox.Show(e.Message,"格物工作台启动失败"); return 1; }
 }
 static string Option(string[] args,string key,string fallback) { int i=Array.IndexOf(args,key);return i>=0&&i+1<args.Length?args[i+1]:fallback; }
 static string Quote(string s) { return "\""+s.Replace("\"","\\\"")+"\""; }
 static Dictionary<string,object> Health(int port) {
  try { var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+port+"/api/health");request.Timeout=1200;
   using(var response=request.GetResponse()) using(var reader=new StreamReader(response.GetResponseStream(),System.Text.Encoding.UTF8)) return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
  } catch { return null; }
 }
 static bool Same(Dictionary<string,object> h,string data) { return h!=null&&h.ContainsKey("application")&&(string)h["application"]=="product-tagging-workbench"&&h.ContainsKey("storage")&&String.Equals(Path.GetFullPath((string)h["storage"]),data,StringComparison.OrdinalIgnoreCase); }
 static bool Occupied(int port) { try { var l=new System.Net.Sockets.TcpListener(IPAddress.Loopback,port);l.Start();l.Stop();return false; } catch { return true; } }
 static int Run(string[] args) {
  var json=new JavaScriptSerializer();string root=AppDomain.CurrentDomain.BaseDirectory;
  string settingsDir=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"GeWuWorkbench");
  string settingsPath=Path.Combine(settingsDir,"desktop.json");Directory.CreateDirectory(settingsDir);
  var settings=File.Exists(settingsPath)?json.Deserialize<Dictionary<string,string>>(File.ReadAllText(settingsPath)):new Dictionary<string,string>();
  string data=Option(args,"--data",settings.ContainsKey("data")?settings["data"]:Path.Combine(settingsDir,"data"));
  string cache=Option(args,"--cache",settings.ContainsKey("cache")?settings["cache"]:Path.Combine(settingsDir,"cache"));
  bool automated=Array.IndexOf(args,"--data")>=0;
  if(!File.Exists(settingsPath)&&!automated) {
   Application.EnableVisualStyles();
   using(var form=new Form()) {
    form.Text="格物工作台 · 首次设置";form.Width=620;form.Height=235;form.StartPosition=FormStartPosition.CenterScreen;
    var d=new TextBox(){Left=125,Top=25,Width=365,Text=data};var c=new TextBox(){Left=125,Top=75,Width=365,Text=cache};
    form.Controls.Add(new Label(){Left=20,Top=28,Width=100,Text="数据存储目录"});form.Controls.Add(d);
    form.Controls.Add(new Label(){Left=20,Top=78,Width=100,Text="缓存目录"});form.Controls.Add(c);
    var bd=new Button(){Left=500,Top=22,Text="选择"};bd.Click+=(s,e)=>{using(var f=new FolderBrowserDialog()){if(f.ShowDialog()==DialogResult.OK)d.Text=f.SelectedPath;}};form.Controls.Add(bd);
    var bc=new Button(){Left=500,Top=72,Text="选择"};bc.Click+=(s,e)=>{using(var f=new FolderBrowserDialog()){if(f.ShowDialog()==DialogResult.OK)c.Text=f.SelectedPath;}};form.Controls.Add(bc);
    form.Controls.Add(new Label(){Left=20,Top=115,Width=550,Text="原表、图片、结果和会话长期保留；缓存软上限10 GB。"});
    var ok=new Button(){Left=440,Top=150,Width=130,Text="保存并打开工作台",DialogResult=DialogResult.OK};form.Controls.Add(ok);form.AcceptButton=ok;
    if(form.ShowDialog()!=DialogResult.OK)return 0;data=d.Text;cache=c.Text;
   }
  }
  data=Path.GetFullPath(data);cache=Path.GetFullPath(cache);Directory.CreateDirectory(data);Directory.CreateDirectory(cache);
  int port=Int32.Parse(Option(args,"--port",settings.ContainsKey("port")?settings["port"]:"8765"));
  string mutexName="Local\\GeWuStart-"+Convert.ToBase64String(System.Security.Cryptography.SHA256.Create().ComputeHash(System.Text.Encoding.UTF8.GetBytes(data.ToLowerInvariant()))).Replace('/','_');
  using(var mutex=new Mutex(false,mutexName)) {
   if(!mutex.WaitOne(45000))throw new Exception("另一个启动过程尚未完成，请稍后重试。");
   try {
    var health=Health(port);
    if(Array.IndexOf(args,"--stop")>=0) { if(Same(health,data)){var r=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+port+"/api/server/stop");r.Method="POST";r.ContentLength=0;r.Timeout=5000;using(var response=r.GetResponse()){} }return 0; }
    if(!Same(health,data)) {
     int end=port+50;while(Occupied(port)&&port<end)port++;if(Occupied(port))throw new Exception("没有可用本机端口。");
     string python=Path.Combine(root,"runtime","pythonw.exe");if(!File.Exists(python))throw new Exception("内置Python运行时缺失，请重新安装。");
     var start=new ProcessStartInfo(python,Quote(Path.Combine(root,"scripts","desktop_bootstrap.py"))) { WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true };
     start.EnvironmentVariables["TAG_STORAGE"]=data;start.EnvironmentVariables["TAG_CACHE"]=cache;start.EnvironmentVariables["TAG_PORT"]=port.ToString();
     start.EnvironmentVariables["TAG_PROVIDER_HOME"]=Path.Combine(data,"providers");start.EnvironmentVariables["REASONIX_EXE"]=Path.Combine(root,"reasonix","reasonix.exe");
     start.EnvironmentVariables["TAG_FRONTEND_DIST"]=Path.Combine(root,"frontend","dist");start.EnvironmentVariables["PYTHONUTF8"]="1";
     start.EnvironmentVariables.Remove("DATABASE_URL");start.EnvironmentVariables.Remove("REASONIX_HOME");
     var process=Process.Start(start);bool ready=false;
     for(int i=0;i<120;i++){Thread.Sleep(500);health=Health(port);if(Same(health,data)){ready=true;break;}if(process.HasExited)break;}
     if(!ready)throw new Exception("工作台启动失败。请查看 "+Path.Combine(data,"logs","desktop.log"));
    }
    if(!automated){settings["data"]=data;settings["cache"]=cache;settings["port"]=port.ToString();File.WriteAllText(settingsPath,json.Serialize(settings));}
    File.WriteAllText(Path.Combine(data,"desktop-endpoint.json"),json.Serialize(new Dictionary<string,object>{{"url","http://127.0.0.1:"+port},{"health",health}}));
    if(Array.IndexOf(args,"--no-open")<0)Process.Start(new ProcessStartInfo("http://127.0.0.1:"+port+"/"){UseShellExecute=true});
   } finally {mutex.ReleaseMutex();}
  }
  return 0;
 }
}
