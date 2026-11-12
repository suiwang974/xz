// preload：两种窗口共用。
//  - 桌宠窗（contextIsolation=false）：只注入 window.petCmd（给宿主发指令），页面保持纯浏览器环境。
//  - 主窗（contextIsolation=true）：经 contextBridge 暴露 window.sniffdlHost，接口与 ui/js/api.js 的期望一致：
//      conn() → {port, token, pid, version}   pickDir(title) → 路径|null   pickFile(filters) → 路径|null
//      saveFile(name, text) → 保存路径|null（宿主弹「另存为」并写入 text）   notify(title, body) → 系统通知
//      show() / quit() / setProgress(p)（api.js 里是可选调用）
const { ipcRenderer, contextBridge } = require('electron');
if (process.contextIsolated) {
  contextBridge.exposeInMainWorld('sniffdlHost', {
    kind: 'electron',
    conn: () => ipcRenderer.invoke('host:conn'),
    pickDir: (title) => ipcRenderer.invoke('host:pickDir', title == null ? '' : String(title)),
    pickFile: (filters) => ipcRenderer.invoke('host:pickFile', filters || null),
    saveFile: (name, text) => ipcRenderer.invoke('host:saveFile', String(name || ''), text == null ? null : (typeof text === 'string' ? text : JSON.stringify(text, null, 2))),
    notify: (title, body) => ipcRenderer.invoke('host:notify', String(title || ''), String(body || '')),
    show: () => ipcRenderer.send('host:show'),
    quit: () => ipcRenderer.send('host:quit'),
    setProgress: (p) => ipcRenderer.send('host:progress', Number(p)),
  });
} else {
  window.petCmd = function (a) { ipcRenderer.send('pet-cmd', String(a)); };
}
