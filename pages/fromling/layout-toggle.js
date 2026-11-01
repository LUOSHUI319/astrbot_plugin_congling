/* 桌面视图 / 手机视图切换
 *
 * 背景：这个面板最早是给电脑用的。手机浏览器打开时，用户一直是「强制缩放」看 PC 布局——
 * 信息密度高，一眼能看到全部。后来加了响应式，窄屏自动变单列，反而和习惯不一致。
 *
 * 所以默认回到「桌面视图」：整页按比例缩放，宽度固定 1280。
 * 响应式规则全部挂在 html[data-layout="mobile"] 下面，desktop 时一条都不生效。
 */
(function () {
  var KEY = "congling.layout";
  var W = 1280;

  function read() {
    try {
      return localStorage.getItem(KEY) || "desktop";
    } catch (e) {
      return "desktop";
    }
  }

  function apply(mode) {
    var root = document.documentElement;
    root.dataset.layout = mode;
    var body = document.body;
    if (!body) return;
    if (mode === "desktop") {
      var vw = window.innerWidth || W;
      var scale = Math.min(1, vw / W);
      body.style.width = W + "px";
      body.style.zoom = String(scale);
      /* zoom 之后 innerHeight 会被放大，反算一下让内容区正好一屏 */
      body.style.minHeight = Math.ceil((window.innerHeight || 800) / (scale || 1)) + "px";
      /* 切换按钮是 fixed 的，会被一起缩小到 0.3 倍，几乎点不到。
         这里反向放大回来，并把 right/bottom 也按比例补偿。 */
      var btn = document.getElementById("layout-toggle");
      if (btn) {
        btn.style.zoom = String(1 / (scale || 1));
        btn.style.right = Math.round(14 / (scale || 1)) + "px";
        btn.style.bottom = Math.round(14 / (scale || 1)) + "px";
      }
    } else {
      body.style.width = "";
      body.style.zoom = "";
      body.style.minHeight = "";
      var b2 = document.getElementById("layout-toggle");
      if (b2) { b2.style.zoom = ""; b2.style.right = ""; b2.style.bottom = ""; }
    }
    paint();
  }

  function paint() {
    var btn = document.getElementById("layout-toggle");
    if (!btn) return;
    var cur = read();
    var text = btn.querySelector(".layout-toggle-text");
    var icon = btn.querySelector(".layout-toggle-icon");
    if (text) text.textContent = cur === "desktop" ? "手机视图" : "桌面视图";
    if (icon) icon.textContent = cur === "desktop" ? "▯" : "▭";
    btn.title = cur === "desktop" ? "切到手机视图（单列、大按钮）" : "切回桌面视图（整页缩放）";
  }

  function toggle() {
    var next = read() === "desktop" ? "mobile" : "desktop";
    try {
      localStorage.setItem(KEY, next);
    } catch (e) {
      /* 存不了也能用，只是刷新后回默认 */
    }
    apply(next);
  }

  function boot() {
    apply(read());
    var btn = document.getElementById("layout-toggle");
    if (btn) btn.addEventListener("click", toggle);
    window.addEventListener("resize", function () {
      if (read() === "desktop") apply("desktop");
    });
  }

  window.njSetLayout = function (mode) {
    try {
      localStorage.setItem(KEY, mode);
    } catch (e) {}
    apply(mode);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
