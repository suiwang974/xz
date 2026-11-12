/* SniffDL A16：页面主世界（MAIN world）小钩子——只做一件事：
 * 页面 JS 程序化触发 <a download> 下载（a.click() / dispatchEvent，常见于"生成后下载"、游离元素不进 DOM）时，
 * 把 {href, download 名} 用 postMessage 告诉内容脚本，后台据此给这条下载补上真实文件名、记作用户发起。
 * 不改页面行为、不拦截、不读页面数据。 */
(() => {
  if (window.__sniffdlPageHook) return;
  window.__sniffdlPageHook = true;
  const post = (a) => {
    try {
      if (!a || !a.href || !a.hasAttribute || !a.hasAttribute("download")) return;
      window.postMessage({ __sniffdl: "dl_hint", url: String(a.href), filename: String(a.getAttribute("download") || "") }, "*");
    } catch (e) {}
  };
  try {
    const P = HTMLAnchorElement.prototype;
    const oc = P.click;
    P.click = function () { post(this); return oc.apply(this, arguments); };
    const od = EventTarget.prototype.dispatchEvent;
    EventTarget.prototype.dispatchEvent = function (ev) {
      try { if (ev && ev.type === "click" && this instanceof HTMLAnchorElement) post(this); } catch (e) {}
      return od.apply(this, arguments);
    };
  } catch (e) {}
})();
