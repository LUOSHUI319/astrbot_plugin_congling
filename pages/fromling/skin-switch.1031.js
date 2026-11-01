/* 浅色 / 深色皮肤切换（零侵入）
   —— 不动 app.js：两套皮肤都挂在 <head> 里，靠 link.disabled 互斥开关。
   深色 = night.1029c.css（原来的夜航手账）
   浅色 = night-light.1031.css（纸色，night.css 的全量换色版）
   选择记在 localStorage 的 congling.skin，跟 app.js 自己那个 congling.look 分开存，
   免得它的 applyLook 每次把 theme 强写回 night。

   另外支持 ?skin=day / ?skin=night，方便预览和截图时直接指定。 */
(function () {
  const KEY = "congling.skin";
  const LINKS = { day: null, night: null };
  const DEFAULT_SKIN = "day";

  function linkOf(skin) {
    if (!LINKS[skin]) LINKS[skin] = document.getElementById("cg-skin-" + skin);
    return LINKS[skin];
  }

  function readSkin() {
    try {
      const q = new URLSearchParams(location.search).get("skin");
      if (q === "day" || q === "night") return q;
      const saved = localStorage.getItem(KEY);
      if (saved === "day" || saved === "night") return saved;
    } catch (err) {
      /* 读不到就用默认 */
    }
    return DEFAULT_SKIN;
  }

  function writeSkin(skin) {
    try {
      localStorage.setItem(KEY, skin);
    } catch (err) {
      /* 存不了就算了，本次照样生效 */
    }
  }

  function apply(skin) {
    const dark = linkOf("night");
    const light = linkOf("day");
    /* 皮肤本身：night 是渲染层认的值，永远保持 night，别动它 */
    const root = document.documentElement;
    root.dataset.ui = "night";
    root.dataset.skin = skin;
    if (dark) dark.disabled = skin !== "night";
    if (light) light.disabled = skin !== "day";
    const box = document.getElementById("cg-skin-pick");
    if (box) {
      box.querySelectorAll("[data-skin]").forEach((btn) => {
        btn.classList.toggle("is-on", btn.dataset.skin === skin);
        btn.setAttribute("aria-pressed", btn.dataset.skin === skin ? "true" : "false");
      });
    }
  }

  function build() {
    const head = document.querySelector(".head-right");
    if (!head || document.getElementById("cg-skin-pick")) return;
    const box = document.createElement("div");
    box.id = "cg-skin-pick";
    box.className = "skin-pick";
    box.title = "纸色 / 夜航";
    box.innerHTML =
      '<button type="button" data-skin="day" title="纸色（浅）" aria-label="纸色"></button>' +
      '<button type="button" data-skin="night" title="夜航（深）" aria-label="夜航"></button>';
    box.querySelectorAll("[data-skin]").forEach((btn) => {
      btn.onclick = (event) => {
        event.stopPropagation();
        const skin = btn.dataset.skin;
        writeSkin(skin);
        apply(skin);
      };
    });
    head.insertBefore(box, head.firstChild);
  }

  /* 首屏那一下已经在 <head> 的内联脚本里定好了，这里只管把按钮挂上 */
  const boot = () => {
    build();            /* 先挂按钮，apply 才好把选中态画上去 */
    apply(readSkin());
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
