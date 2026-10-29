// The only bridge between the sandboxed page and the rest of the app. Every call is
// forwarded to the main process, which validates it; no Node API reaches the page.
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

const api = {
  engineInfo: () => ipcRenderer.invoke("engine:info"),
  restartEngine: () => ipcRenderer.invoke("engine:restart"),
  call: (method: string, path: string, body?: unknown) => ipcRenderer.invoke("api", method, path, body),
  pdf: (path: string) => ipcRenderer.invoke("api:pdf", path),
  subscribe: (projectId: string) => ipcRenderer.invoke("events:subscribe", projectId),
  onEvent: (cb: (ev: unknown) => void) => {
    const listener = (_e: IpcRendererEvent, ev: unknown) => cb(ev);
    ipcRenderer.on("engine:event", listener);
    return () => {
      ipcRenderer.removeListener("engine:event", listener);
    };
  },
  pickDocx: () => ipcRenderer.invoke("dialog:docx"),
  pickFolder: (title: string) => ipcRenderer.invoke("dialog:folder", title),
  openExternal: (url: string) => ipcRenderer.invoke("shell:open", url),
  showFolder: (folder: string) => ipcRenderer.invoke("shell:show", folder),
  openLogs: () => ipcRenderer.invoke("logs:open"),
  copy: (text: string) => ipcRenderer.invoke("clipboard:write", text),
};

contextBridge.exposeInMainWorld("livery", api);
export type LiveryBridge = typeof api;
