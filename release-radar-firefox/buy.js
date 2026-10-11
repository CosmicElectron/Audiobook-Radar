// Release Radar — auto-buy (KU borrow -> companion audiobook -> return borrow).
//
// The app decides; this file does. Three phases, each reported back to
// POST /api/buy/report so the app's job record is the single source of truth:
//   probe    fetch the Kindle (and audiobook) page SIGNED IN, read: signed in?
//            Kindle Unlimited title? companion-audiobook price? already owned /
//            borrowed? which buttons exist. Pure read, nothing is clicked.
//   dry      open the Kindle page in a background tab, run the page step in
//            look-only mode, report exactly what WOULD be clicked. Nothing is.
//   execute  tick the audiobook add-on, click the KU borrow, wait for the
//            confirmation page, read it back, then return the borrow through
//            the same call KU Central's Return button makes. Every step reports;
//            any doubt stops the run (the app marks it and never auto-retries).
//
// Hard rules: never click when the page is a sign-in/robot page, when the
// price on the page is not the price the app approved, when zero or more than
// one candidate button matches, or when the audiobook ASIN on the page is not
// the one in the job.

var RR_BUY_REPORT_PATH = "/api/buy/report";

// ---------------------------------------------------------------- helpers
function _rrClean(s) { return (s || "").replace(/\s+/g, " ").trim(); }
function _rrPrice(txt) {
  // "$7.47" / "AU$7.47" / "7,47 €" -> number
  var m = (txt || "").match(/(?:AU\$|A\$|US\$|\$|£|€)\s*(\d{1,4}(?:[.,]\d{2})?)/) || (txt || "").match(/(\d{1,4}[.,]\d{2})\s*(?:€|AUD|USD)/);
  if (!m) return null;
  var v = parseFloat(m[1].replace(",", "."));
  return isFinite(v) ? v : null;
}
function _rrCurrency(txt) {
  if (/AU\$|AUD/.test(txt || "")) return "AUD";
  if (/US\$|USD/.test(txt || "")) return "USD";
  if (/£/.test(txt || "")) return "GBP";
  if (/€/.test(txt || "")) return "EUR";
  return /\$/.test(txt || "") ? "$" : "";
}

function _rrUp(el, pred, hops) {           // nearest ancestor-or-self matching pred
  var n = 0; while (el && n++ < (hops || 12)) { try { if (pred(el)) return el; } catch (e) {} el = el.parentElement; } return null;
}
function _rrHasClass(el, c) { var cls = " " + (typeof el.className === "string" ? el.className : "") + " "; return cls.indexOf(" " + c + " ") >= 0; }

// ---------------------------------------------------------------- probe (pure)
// Works on a DOMParser document (background fetch) of a Kindle OR audiobook page.
function RR_buyProbe(doc, url) {
  var out = { url: url || "", signed_in: null, robot: false, page: "unknown", asin: null,
              ku: null, borrowed: null, owned_audio: null, price: null, currency: "", price_text: "",
              audio_asin: null, kindle_asin: null, buttons: [], excerpt: "" };
  try {
    var title = doc.title || "";
    out.robot = !!doc.querySelector('#captchacharacters, form[action*="validateCaptcha"]') || /robot check/i.test(title);
    if (/sign in|sign-in/i.test(title) || doc.querySelector('#ap_email, form[name="signIn"]')) { out.signed_in = false; out.page = "signin"; return out; }
    var nav = doc.querySelector("#nav-link-accountList-nav-line-1");
    var navTxt = _rrClean(nav && nav.textContent);
    if (navTxt) out.signed_in = !/hello, sign in|^sign in$/i.test(navTxt);
    var m = (url || "").match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/);
    out.asin = m ? m[1] : null;
    var asinInput = doc.querySelector('input#ASIN, input[name="ASIN"], input[name="asin"]');
    if (asinInput && asinInput.getAttribute("value")) out.asin = asinInput.getAttribute("value");

    // which kind of page
    var isAudio = !!(doc.querySelector("#audibleProductDetails, #buyBoxAccordion, #audibleBuyButton, #adbl_buy_button") ||
                     /audible audiobook/i.test(_rrClean((doc.querySelector("#productSubtitle, #title") || {}).textContent)));
    var isKindle = !!(doc.querySelector("#buyOneClick, #kuUpsell_desktop_feature_div, #Kibbo-KINDLE_ALC-Desktop, #tmm-grid-swatch-KINDLE.selected") ||
                      /kindle edition/i.test(_rrClean((doc.querySelector("#productSubtitle, #title") || {}).textContent)));
    out.page = isAudio ? "audio" : (isKindle ? "kindle" : "unknown");

    // formats: the OTHER edition's ASIN
    doc.querySelectorAll('#tmmSwatches a[href], #formats a[href], [id^="tmm-grid-swatch-"] a[href]').forEach(function (a) {
      var hm = (a.getAttribute("href") || "").match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/);
      if (!hm) return;
      var href = a.getAttribute("href") || "", t = _rrClean(a.textContent).toLowerCase();
      var sw = _rrUp(a, function (x) { return /^tmm-grid-swatch-/.test(x.id || ""); });
      var swId = sw ? (sw.id || "") : "";
      if (/tmm_kin_swatch/.test(href) || /KINDLE/.test(swId) || /^kindle/.test(t)) { if (!out.kindle_asin) out.kindle_asin = hm[1]; }
      else if (/tmm_aud_swatch/.test(href) || /AUDIO_DOWNLOAD/.test(swId) || /audible|audiobook/.test(t)) { if (!out.audio_asin && !/audio cd|mp3 cd/.test(t)) out.audio_asin = hm[1]; }
    });
    if (out.page === "audio" && !out.audio_asin) out.audio_asin = out.asin;
    if (out.page === "kindle" && !out.kindle_asin) out.kindle_asin = out.asin;

    // the buy box
    var scope = null;
    ['#buybox', '#desktop_buybox', '#CombinedBuybox', '#Unified-Buybox-Container', '#buyBoxAccordion', '#rightCol'].some(function (sel) {
      scope = doc.querySelector(sel); return !!scope;
    });
    var root = scope || doc.body || doc.documentElement;
    function vis(node) {          // visible-ish: not inside an aok-hidden/hidden/display:none ancestor
      var el = node, hops = 0;
      while (el && el !== root && hops++ < 40) {
        var cls = " " + (typeof el.className === "string" ? el.className : "") + " ";
        if (cls.indexOf(" aok-hidden ") >= 0 || cls.indexOf(" a-hidden ") >= 0 || cls.indexOf(" hidden ") >= 0) return false;
        if (el.getAttribute && (el.getAttribute("hidden") !== null || el.getAttribute("aria-hidden") === "true")) return false;
        var st = el.getAttribute && (el.getAttribute("style") || "");
        if (/display\s*:\s*none|visibility\s*:\s*hidden/i.test(st)) return false;
        el = el.parentElement;
      }
      return true;
    }
    function visText(r, cap) {
      var s = "", n = 0;
      try {
        var tw = doc.createTreeWalker(r, 4), tn;
        while ((tn = tw.nextNode()) && n++ < (cap || 8000)) {
          var pe = tn.parentElement, tag = pe && pe.tagName;
          if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE") continue;
          if (!vis(pe)) continue;
          var t = _rrClean(tn.nodeValue); if (t) s += " " + t;
        }
      } catch (e) {}
      return s;
    }
    var text = visText(root);
    out.excerpt = text.slice(0, 600);
    try {   // raw markup of the buy box for offline inspection (stored only by the local app)
      var parts = [];
      if (scope) parts.push(scope.outerHTML || "");
      var extra = doc.querySelector("#audibleNarration, #adblWhispersyncForVoice_feature_div, #kuUpsell_desktop_feature_div");
      if (extra) parts.push(extra.outerHTML || "");
      out.buybox_html = parts.join("\n<!-- ---- -->\n").slice(0, 120000);
    } catch (e) {}

    // Kindle Unlimited?
    var kuSwatch = doc.querySelector("#tmm-grid-swatch-KINDLE");
    var kuSwText = kuSwatch ? _rrClean(kuSwatch.textContent) : "";
    if (doc.querySelector(".a-icon-kindle-unlimited, #kuUpsell_desktop_feature_div, #Kibbo-KINDLE_UNLIMITED_UPSELL-Desktop") ||
        /with kindle unlimited|read for free|kindle unlimited/i.test(kuSwText + " " + text)) out.ku = true;
    else if (out.page === "kindle") out.ku = false;
    if (/you borrowed this|you're borrowing|return this book|borrowed on |manage your kindle unlimited titles|already borrowed this title/i.test(text)) out.borrowed = true;
    if (/you (?:already )?own (?:this|the audiobook)|in your (?:audible )?library|already (?:purchased|in your)|you purchased this/i.test(text)) out.owned_audio = true;

    // companion price: the narration block on the Kindle page, or the plain
    // sentence on the audiobook page
    var narr = doc.querySelector("#audibleNarration, #audible-narration, [id*='addNarration' i], [id*='narration' i]");
    var ptxt = "";
    if (narr && vis(narr)) {
      var pe2 = narr.querySelector(".a-color-price");
      ptxt = _rrClean(pe2 ? pe2.textContent : narr.textContent);
      if (!_rrPrice(ptxt)) ptxt = _rrClean(narr.textContent);
      var aa = doc.querySelector("#audibleAsin");
      var aav = aa && aa.getAttribute("value");
      if (aav && /^[A-Z0-9]{10}$/.test(aav)) out.audio_asin = out.audio_asin || aav;
      var inl = (narr.innerHTML || "").match(/audibleAsin['"]?\)\.val\(["']([A-Z0-9]{10})["']\)/);
      if (!out.audio_asin && inl) out.audio_asin = inl[1];
    }
    if (!_rrPrice(ptxt)) {
      var wfv = doc.querySelector("#adblWhispersyncForVoice_feature_div, #adblWhispersyncForVoice, [id*='whispersync' i]");
      var wtxt = wfv ? visText(wfv, 800) : "";
      var wm = wtxt.match(/add (?:the )?(?:audible )?(?:audiobook|narration)[^$£€]{0,80}?(?:for (?:a reduced price of |just |only )?)((?:AU\$|A\$|US\$|\$|£|€)\s*\d{1,4}(?:[.,]\d{2})?)/i);
      if (wm) ptxt = wm[0];
    }
    if (!_rrPrice(ptxt)) {
      var mm = text.match(/add (?:the )?(?:audible )?(?:audiobook|narration)[^$£€]{0,80}?(?:for (?:a reduced price of |just |only )?)((?:AU\$|A\$|US\$|\$|£|€)\s*\d{1,4}(?:[.,]\d{2})?)/i);
      if (mm) ptxt = mm[0];
    }
    if (!_rrPrice(ptxt)) {
      var ap = doc.querySelector("#audibleOurPrice");
      var apv = ap && ap.getAttribute("value");
      if (apv && /^\d+(?:\.\d+)?$/.test(apv) && parseFloat(apv) > 0) ptxt = "$" + apv;
    }
    out.price = _rrPrice(ptxt);
    out.currency = _rrCurrency(ptxt);
    out.price_text = _rrClean(ptxt).slice(0, 120);

    // buttons in the buy box, with their homes
    var seen = {};
    root.querySelectorAll('input[type="submit"], button, .a-button-input, a.a-button-text').forEach(function (b) {
      if (out.buttons.length >= 14 || !vis(b)) return;
      var lab = _rrClean(b.getAttribute("value") || b.getAttribute("aria-label") || b.textContent || "");
      var ann = b.parentElement && b.parentElement.querySelector && b.parentElement.querySelector("[id$='-announce']");
      if (!lab && ann) lab = _rrClean(ann.textContent);
      if (!lab || lab.length > 70) return;
      var host = _rrUp(b, function (x) { return x.tagName === "SPAN" && _rrHasClass(x, "a-button"); }, 6) || b;
      var form = _rrUp(b, function (x) { return x.tagName === "FORM"; }, 30);
      var key = (host.id || "") + "|" + lab;
      if (seen[key]) return; seen[key] = 1;
      out.buttons.push({ id: host.id || b.id || "", name: b.getAttribute("name") || "", label: lab.slice(0, 70),
                         form: form ? (form.id || "") : "", action: form ? (form.getAttribute("action") || "").slice(0, 120) : "" });
    });
  } catch (e) { out.error = String(e); }
  return out;
}

// ---------------------------------------------------------- page step (content)
// Runs INSIDE the Kindle page tab (serialised with toString, so it must be
// self-contained). mode: "look" (report only) | "borrow" (tick + click) |
// "confirm" (read the page after navigation) | "narration" (click a standalone
// "Add audiobook for $X" if the borrow form did not carry it).
function RR_pageStep(opts) {
  var res = { mode: opts.mode, ok: false, clicked: false, info: "", found: {} };
  var cfg = opts.cfg || {};
  var RX = function (k, d) { try { return new RegExp(cfg[k] || d, "i"); } catch (e) { return new RegExp(d, "i"); } };
  var SEL = function (k, d) { return (cfg[k] || d); };
  var BORROW_LABEL = RX("borrow_label", "add to library|read for free|^borrow"), BORROW_ID = RX("borrow_id", "borrow|kindle-unlimited|ku-?borrow|kubuy");
  var BUY_LABEL = RX("buy_label", "^buy now with 1-click|^buy now$|^buy for|^pre-order");
  var ADD_AUDIO_LABEL = RX("add_audio_label", "add (?:the )?audiobook|add (?:audible )?narration|upgrade with audio|get the audiobook|add narration");
  var NARR_CHK = SEL("narr_chk", "#narration-checkbox, input[type='checkbox'][id*='narration' i], input[type='checkbox'][name*='narration' i]");
  var NARR_BLOCK = SEL("narr_block", "#audibleNarration, #audible-narration, [id*='narration' i]");
  var EXPANDER = SEL("expander", "");
  function clean(s) { return (s || "").replace(/\s+/g, " ").trim(); }
  function price(t) { var m = (t || "").match(/(?:AU\$|A\$|US\$|\$|£|€)\s*(\d{1,4}(?:[.,]\d{2})?)/); return m ? parseFloat(m[1].replace(",", ".")) : null; }
  function visible(el) { if (!el) return false; var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null; if (r && (r.width === 0 && r.height === 0)) return false; var s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; }
  function labelOf(b) { var l = clean(b.getAttribute("value") || b.getAttribute("aria-label") || b.textContent || ""); if (!l) { var a = b.parentElement && b.parentElement.querySelector("[id$='-announce']"); if (a) l = clean(a.textContent); } return l; }
  try {
    var title = document.title || "";
    if (/sign in|sign-in/i.test(title) || document.querySelector('#ap_email, form[name="signIn"]')) { res.info = "sign-in page"; return res; }
    if (document.querySelector('#captchacharacters, form[action*="validateCaptcha"]') || /robot check/i.test(title)) { res.info = "robot check page"; return res; }
    var bodyText = clean(document.body ? document.body.innerText : "");

    if (opts.mode === "confirm") {
      // after the click: what did the page become?
      var ok = /thank you|thanks for your (?:order|purchase)|order (?:has been )?placed|added to your library|has been added|you now own|enjoy your/i.test(bodyText);
      var mentionsAudio = /audiobook|audible|narration/i.test(bodyText);
      // Evidence that THIS purchase went through, not just any thank-you page.
      // Audible's page says "Thank you for choosing <title>" and links the
      // audiobook, but shows no price - and its "you might like" carousel is
      // full of "$16.45/mo" membership prices (the first price on the page was
      // once reported as if it were the charge, Riftside 4, 29 Sep 2026).
      var low = bodyText.toLowerCase();
      var tFull = clean(opts.title || "").toLowerCase(), tShort = tFull.split(":")[0].trim();
      var titleSeen = !!tFull && (low.indexOf(tFull) >= 0 || (tShort.length >= 6 && low.indexOf(tShort) >= 0));
      var html = ""; try { html = document.documentElement.outerHTML || ""; } catch (e) {}
      var asinSeen = !!opts.audio_asin && html.indexOf(opts.audio_asin) >= 0;
      var priceSeen = opts.price != null && new RegExp("\\$\\s?" + Number(opts.price).toFixed(2).replace(".", "\\.") + "(?!\\d)").test(bodyText);
      res.found = { url: location.href, title: title.slice(0, 120), thanks: ok, audio: mentionsAudio, title_seen: titleSeen,
                    asin_seen: asinSeen, price_seen: priceSeen, excerpt: bodyText.slice(0, 500) };
      res.html = html.slice(0, 200000);
      // a KU borrow's thank-you page names the same title, so the title alone
      // only counts when the page is about the audiobook
      res.ok = ok && (asinSeen || priceSeen || (titleSeen && mentionsAudio));
      var ev = [];
      if (titleSeen) ev.push("names this book"); if (asinSeen) ev.push("links this audiobook"); if (priceSeen) ev.push("shows the $" + Number(opts.price).toFixed(2));
      res.info = (ok ? "confirmation page" : "no confirmation text") +
                 (ev.length ? (", " + ev.join(", ")) : (ok ? ", but it doesn't name this audiobook or show its price" : ""));
      return res;
    }

    // -- the KU borrow widget's own context (ASIN, CSRF token, endpoints), when present
    var kuctx = null;
    try {
      var scs = document.querySelectorAll("script");
      for (var si = 0; si < scs.length && !kuctx; si++) {
        var st = scs[si].textContent || "";
        if (st.indexOf("KU-DP-BottomSheet-Context") < 0) continue;
        var pick = function (k) { var mm = st.match(new RegExp("[\"']" + k + "[\"']\\s*:\\s*[\"']([^\"']*)[\"']")); return mm ? mm[1] : null; };
        kuctx = { asin: pick("ASIN"), program: pick("PROGRAM"), channel: pick("CHANNEL"), subType: pick("BORROW_SUB_TYPE"), csrf: pick("CSRF_TOKEN"),
                  borrowRef: pick("BORROW_BUTTON_REF_TAG"), typ: pick("TYP_BASE_URL"), rnbEndpoint: pick("RETURN_AND_BORROW_ENDPOINT"),
                  action: pick("CONTINUE_ACTION") };
      }
    } catch (e) {}
    res.found.kuctx = kuctx ? { asin: kuctx.asin, program: kuctx.program, has_csrf: !!kuctx.csrf, typ: kuctx.typ, rnb: kuctx.rnbEndpoint, ref: kuctx.borrowRef } : null;
    res.kuctx = kuctx;
    // -- locate the pieces
    var scope = document.querySelector('#buybox, #desktop_buybox, #CombinedBuybox, #Unified-Buybox-Container, #rightCol') || document.body;
    var all = Array.prototype.slice.call(scope.querySelectorAll('input[type="submit"], button, .a-button-input, a.a-button-text'));
    // an optional expander (e.g. the small arrow under the KU button) that reveals the audiobook offer
    if (EXPANDER && opts.mode !== "confirm") {
      try { var ex = document.querySelector(EXPANDER); if (ex && visible(ex)) { ex.click(); res.found.expanded = true; } } catch (e) {}
    }
    var borrowBtns = all.filter(function (b) { if (!visible(b)) return false; var l = labelOf(b).toLowerCase(); var host = b.closest("span.a-button, div") || b; var id = ((host.id || "") + " " + (b.id || "") + " " + (b.getAttribute("name") || "")).toLowerCase(); return BORROW_LABEL.test(l) || BORROW_ID.test(id); });
    var buyBtns = all.filter(function (b) { if (!visible(b)) return false; var l = labelOf(b).toLowerCase(); return BUY_LABEL.test(l); });
    var narrChk = document.querySelector(NARR_CHK);
    var narrBlock = document.querySelector(NARR_BLOCK);
    var narrText = narrBlock ? clean(narrBlock.innerText || narrBlock.textContent) : "";
    var narrPrice = price(narrText);
    var narrAsinEl = document.querySelector("#audibleAsin");
    var narrAsin = narrAsinEl ? narrAsinEl.value : null;
    if (!narrAsin && narrBlock) { var im = (narrBlock.innerHTML || "").match(/audibleAsin['"]?\)\.val\(["']([A-Z0-9]{10})["']\)/); if (im) narrAsin = im[1]; }
    var addAudioBtns = all.filter(function (b) { if (!visible(b)) return false; var l = labelOf(b).toLowerCase(); return ADD_AUDIO_LABEL.test(l); });
    try { res.html = (scope.outerHTML || "").slice(0, 120000); } catch (e) {}
    // "Read Now" instead of a borrow button: the Kindle edition is already in
    // the library (borrowed earlier, or owned). Nothing to borrow - and nothing
    // of ours to return afterwards (Riftside 4, 29 Sep 2026, stopped here).
    var IN_LIB = RX("in_library_label", "^read now\\b|^read (?:it )?in the (?:free )?kindle app");
    var readNowBtns = all.filter(function (b) { return visible(b) && IN_LIB.test(labelOf(b).toLowerCase()); });
    var sameForm = !!(narrChk && borrowBtns.length === 1 && narrChk.form && borrowBtns[0].form && narrChk.form === borrowBtns[0].form);
    res.found = { same_form: sameForm,
                  borrow: borrowBtns.map(function (b) { return { id: (b.closest("span.a-button") || b).id || b.id || "", label: labelOf(b) }; }),
                  buy: buyBtns.map(function (b) { return { id: (b.closest("span.a-button") || b).id || b.id || "", label: labelOf(b) }; }),
                  add_audio: addAudioBtns.map(function (b) { return { id: (b.closest("span.a-button") || b).id || b.id || "", label: labelOf(b) }; }),
                  narration_checkbox: !!narrChk, narration_visible: !!(narrBlock && visible(narrBlock)), narration_price: narrPrice, narration_asin: narrAsin,
                  narration_text: narrText.slice(0, 160), borrowed_already: /you borrowed this|return this book|you're borrowing|borrowed on \w+|manage your kindle unlimited titles|already borrowed this title/i.test(bodyText),
                  in_library: readNowBtns.length > 0 && borrowBtns.length === 0,
                  url: location.href };
    var skipBorrow = !!(res.found.borrowed_already || res.found.in_library);

    // -- checks common to look/borrow
    var problems = [];
    if (opts.audio_asin && narrAsin && narrAsin !== opts.audio_asin) problems.push("audiobook ASIN on page " + narrAsin + " != job " + opts.audio_asin);
    if (opts.price != null && narrPrice != null && Math.abs(narrPrice - opts.price) > 0.011) problems.push("price on page $" + narrPrice + " != approved $" + opts.price);
    if (opts.cap != null && narrPrice != null && narrPrice > opts.cap + 0.001) problems.push("price $" + narrPrice + " above cap $" + opts.cap);

    if (opts.mode === "checkout" || opts.mode === "confirm") {
      // Amazon's own refusal (visible alert) - never keep pressing through it
      var alerts = Array.prototype.slice.call(document.querySelectorAll('#audibleCheckoutErrorMessage, .a-alert-error, .a-alert-warning, .a-alert-info'));
      for (var ai = 0; ai < alerts.length; ai++) {
        var al = alerts[ai]; if (!visible(al)) continue;
        var at = clean(al.innerText || al.textContent || "");
        if (/error occurred|we're sorry|unable to (?:process|complete)|try again later|exceeded the maximum number of attempts|could not (?:be )?process/i.test(at)) {
          res.blocked = true; res.found = { url: location.href, alert: at.slice(0, 300) };
          res.info = "Amazon refused: " + at.slice(0, 200);
          return res;
        }
      }
    }
    if (opts.mode === "checkout") {
      // After "Buy Now" the Audible checkout shows (1) "Select a payment method"
      // with the saved card and a Continue button, then (2) a review page with
      // one finish button and the price. Click (1) only with a card already
      // selected; click (2) only when exactly one finish button exists and the
      // approved price is on the page.
      if (/select a payment method/i.test(bodyText)) {
        var allc = Array.prototype.slice.call(document.querySelectorAll('input[type="submit"], button, .a-button-input'));
        var cont = allc.filter(function (b) { return visible(b) && (/^continue$/i.test(labelOf(b)) || /SetPaymentPlanSelectContinueEvent/.test(b.getAttribute("name") || "")); });
        var radios = Array.prototype.slice.call(document.querySelectorAll('input[type="radio"][name="ppw-instrumentRowSelection"], input[type="radio"][name*="instrument" i]'));
        var checked = radios.filter(function (r) { return r.checked; });
        res.found = { stage: "payment", continue_buttons: cont.length, payment_methods: radios.length, selected: checked.length, url: location.href };
        res.stage = "payment";
        if (cont.length !== 1) { res.info = "payment page: " + (cont.length ? "more than one Continue button" : "no Continue button"); return res; }
        if (!checked.length) {
          if (radios.length === 1) { radios[0].click(); checked = radios.filter(function (r) { return r.checked; }); }
          if (!checked.length) { res.info = "payment page: no payment method selected (" + radios.length + " on file) - refusing to guess"; return res; }
        }
        res.ok = true; res.info = "payment page: card on file selected, 'Continue'";
        if (opts.commit) { cont[0].click(); res.clicked = true; res.info = "payment page: pressed Continue with the card on file"; }
        return res;
      }
      var FINISH = RX("checkout_label", "place (?:your )?order|complete (?:your )?purchase|^buy now$|confirm (?:your )?(?:purchase|order)|pay now");
      var fins = Array.prototype.slice.call(document.querySelectorAll('input[type="submit"], button, .a-button-input, a.a-button-text')).filter(function (b) { return visible(b) && FINISH.test(labelOf(b).toLowerCase()); });
      var pageHasPrice = opts.price != null && new RegExp("\\$\\s?" + String(opts.price.toFixed(2)).replace(".", "\\.")).test(bodyText);
      res.found = { finish: fins.map(function (b) { return { id: (b.closest("span.a-button") || b).id || b.id || "", label: labelOf(b) }; }), price_on_page: pageHasPrice, url: location.href, excerpt: bodyText.slice(0, 300) };
      try { res.html = (document.documentElement.outerHTML || "").slice(0, 200000); } catch (e) {}
      if (fins.length !== 1) { res.info = fins.length ? ("AMBIGUOUS: " + fins.length + " finish buttons") : "no review/finish button on this page"; return res; }
      if (!pageHasPrice) { res.info = "review page does not show $" + opts.price + " - refusing"; return res; }
      res.ok = true; res.stage = "review"; res.info = "review page: '" + labelOf(fins[0]) + "' with $" + opts.price + " shown";
      if (opts.commit) { fins[0].click(); res.clicked = true; res.info = "clicked '" + labelOf(fins[0]) + "' on the review page"; }
      return res;
    }

    if (opts.mode === "offer" || opts.mode === "narration") {
      // The audiobook offer, wherever this page shows it: an "Add the audiobook
      // for $X" control, or (audiobook page after the borrow) the buy button
      // next to the companion price. A button is only a candidate when a price
      // is visible with it and equals the approved price.
      function nearText(b) {
        // the price that belongs to this button: its accordion row (header + body) first,
        // then its form, then the nearest div
        var row = b.closest("[data-a-accordion-row-name], .a-accordion-row-container");
        if (row) { var rt = clean(row.innerText || row.textContent || ""); if (price(rt) != null) return rt; }
        var f = b.closest("form") || b.closest("span.a-button") || b; var t = clean(f.innerText || f.textContent || "");
        var h = b.closest("div"); if (h && price(t) == null) t = clean(h.innerText || ""); return t;
      }
      var boxText = clean(scope.innerText || scope.textContent || "").slice(0, 4000);
      var boxPrice = price(boxText);
      var cands = [];
      addAudioBtns.forEach(function (b) { var pp = price(labelOf(b) + " " + nearText(b) + " " + narrText); if (pp == null) pp = boxPrice; cands.push({ b: b, price: pp, via: "add-audiobook" }); });
      buyBtns.forEach(function (b) { var pp = price(nearText(b)); if (pp == null) pp = boxPrice; if (opts.price != null && pp != null && Math.abs(pp - opts.price) <= 0.011) cands.push({ b: b, price: pp, via: "buy-button at the companion price" }); });
      res.found.offer = cands.map(function (c) { return { id: (c.b.closest("span.a-button") || c.b).id || c.b.id || "", label: labelOf(c.b), price: c.price, via: c.via }; });
      res.found.box_price = boxPrice;
      if (cands.length === 0 && !opts.no_expand) {
        // Amazon's buy box is an accordion: the cash row ("Buy -84% $7.47") may be
        // collapsed behind the membership row. Expand the ONE row whose header
        // carries the approved price, then the caller re-checks.
        var hdrs = Array.prototype.slice.call(document.querySelectorAll('[data-action="a-accordion"][aria-expanded="false"], .a-accordion-row-a11y[aria-expanded="false"]'));
        var hit = hdrs.filter(function (h) {
          var row = h.closest("[data-a-accordion-row-name]") || h.parentElement || h;
          var t = clean(row.innerText || row.textContent || "");
          var pp = price(clean(h.innerText || h.textContent || "")); if (pp == null) pp = price(t);
          return visible(h) && /\bbuy\b/i.test(t) && opts.price != null && pp != null && Math.abs(pp - opts.price) <= 0.011;
        });
        res.found.collapsed_rows = hdrs.length;
        if (hit.length === 1) {
          try { hit[0].scrollIntoView({ block: "center" }); } catch (e) {}
          ["mousedown", "mouseup", "click"].forEach(function (ev) { try { hit[0].dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true, view: window, button: 0 })); } catch (e) {} });
          res.expanded = true;
          res.info = "expanded the $" + opts.price + " buying option - re-checking";
          return res;
        }
      }
      if (cands.length !== 1) { res.info = cands.length ? ("AMBIGUOUS: " + cands.length + " offer buttons") : ("no audiobook offer on this page" + (boxPrice != null ? (" (buy box shows $" + boxPrice + ")") : "")); return res; }
      var cand = cands[0];
      if (cand.price == null) { res.info = "offer button '" + labelOf(cand.b) + "' has no visible price - refusing"; return res; }
      if (opts.price != null && Math.abs(cand.price - opts.price) > 0.011) { res.info = "offer price $" + cand.price + " != approved $" + opts.price; return res; }
      if (opts.cap != null && cand.price > opts.cap + 0.001) { res.info = "offer price $" + cand.price + " above cap $" + opts.cap; return res; }
      if (problems.length) { res.info = problems.join("; "); return res; }
      res.ok = true;
      res.info = "offer: '" + labelOf(cand.b) + "' (" + cand.via + ") at $" + cand.price;
      if (opts.commit) { cand.b.click(); res.clicked = true; res.info = "clicked '" + labelOf(cand.b) + "' at $" + cand.price; }
      return res;
    }

    // borrow path
    var plan = [];
    if (skipBorrow) plan.push(res.found.in_library ? "already in your Kindle library ('Read Now') - no borrow needed" : "already borrowed - skip the borrow");
    else if (borrowBtns.length === 1) plan.push("borrow: click '" + labelOf(borrowBtns[0]) + "' (" + (res.found.borrow[0].id || "no id") + ")");
    else plan.push(borrowBtns.length ? ("AMBIGUOUS: " + borrowBtns.length + " borrow buttons") : "NO borrow / Add-to-library button found");
    if (narrChk && sameForm) plan.push("tick #" + (narrChk.id || "narration checkbox") + (narrPrice != null ? (" ($" + narrPrice + ")") : "") + " in the borrow form");
    else plan.push("audiobook offer expected AFTER the borrow");
    res.info = plan.join("; ") + (problems.length ? (" | PROBLEMS: " + problems.join("; ")) : "");

    if (opts.mode === "look") { res.ok = (borrowBtns.length === 1 || skipBorrow) && !problems.length; return res; }

    // mode === "borrow": really do it (a KU borrow is free and is returned at the end)
    if (problems.length) return res;
    if (!skipBorrow && borrowBtns.length !== 1) return res;
    var useChk = !!(narrChk && sameForm);
    if (useChk && !narrChk.checked) { narrChk.click(); if (!narrChk.checked) { narrChk.checked = true; narrChk.dispatchEvent(new Event("change", { bubbles: true })); } }
    if (useChk && !narrChk.checked) { res.info = "could not tick the audiobook checkbox"; return res; }
    if (skipBorrow) { res.ok = true; res.info = (res.found.in_library ? "already in your Kindle library ('Read Now') - no borrow needed" : "already borrowed") + " - looking for the audiobook offer"; res.needs_offer = true; return res; }
    // the KU button is a JS widget, not a form submit: give it a real mouse
    // sequence on the inner button AND its a-button host, then let the page act
    var tgt = borrowBtns[0], hostSpan = tgt.closest("span.a-button") || tgt;
    try { tgt.scrollIntoView({ block: "center" }); } catch (e) {}
    try { tgt.focus(); } catch (e) {}
    ["mousedown", "mouseup", "click"].forEach(function (ev) {
      try { tgt.dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true, view: window, button: 0 })); } catch (e) {}
    });
    try { if (tgt.tagName === "INPUT" && tgt.form) { /* a real submit would have navigated already */ } } catch (e) {}
    res.clicked = true; res.ok = true; res.needs_offer = !useChk;
    res.info = "clicked '" + labelOf(borrowBtns[0]) + "'" + (useChk ? " with the audiobook add-on ticked" : " - now looking for the audiobook offer");
    return res;
  } catch (e) { res.info = "error: " + String(e); return res; }
}

// ------------------------------------------------------------- background side
// Everything below runs in the extension background page and needs RADAR_BASE,
// fetchTimeout, logLine from background.js (same global scope).
var _rrBuyChain = Promise.resolve();

function RR_buyEnqueue(cmd) {
  _rrBuyChain = _rrBuyChain.then(function () { return RR_runBuy(cmd); }).catch(function (e) { try { logLine("buy: " + String(e)); } catch (_) {} });
  return _rrBuyChain;
}

async function _rrReport(jobId, phase, ok, detail) {
  try {
    await fetchTimeout(RADAR_BASE + RR_BUY_REPORT_PATH, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, phase: phase, ok: !!ok, detail: detail || {} }) }, 15000);
  } catch (e) { logLine("buy: could not report " + phase + ": " + String(e)); }
}

async function _rrFetchDoc(url) {
  var resp = await fetchTimeout(url, { credentials: "include", headers: { "Accept": "text/html,application/xhtml+xml" } }, 45000);
  if (!resp.ok) throw new Error("http " + resp.status);
  return new DOMParser().parseFromString(await resp.text(), "text/html");
}

function _rrUrl(host, asin) { return "https://" + (host || "www.amazon.com.au") + "/dp/" + asin; }

async function RR_probe(job) {
  var host = job.store_host || "www.amazon.com.au";
  var detail = { info: "" };
  var kindle = job.kindle_asin, audio = job.audio_asin;
  var kp = null, ap = null;
  // the audiobook page: companion-price sentence + the Kindle edition link
  if (audio) {
    try { ap = RR_buyProbe(await _rrFetchDoc(_rrUrl(host, audio)), _rrUrl(host, audio)); } catch (e) { detail.info += "audio page: " + e + "; "; }
    if (ap && !kindle && ap.kindle_asin) kindle = ap.kindle_asin;
  }
  if (kindle) {
    try { kp = RR_buyProbe(await _rrFetchDoc(_rrUrl(host, kindle)), _rrUrl(host, kindle)); } catch (e) { detail.info += "kindle page: " + e + "; "; }
  }
  var src = kp || ap;
  if (!src) { detail.info += "could not read either page"; return { ok: false, detail: detail }; }
  detail.signed_in = (kp && kp.signed_in != null) ? kp.signed_in : (ap ? ap.signed_in : null);
  detail.robot = !!((kp && kp.robot) || (ap && ap.robot));
  detail.kindle_asin = kindle || null;
  detail.audio_asin = audio || (kp && kp.audio_asin) || null;
  detail.ku = kp ? kp.ku : null;
  detail.borrowed = kp ? kp.borrowed : null;
  detail.owned = !!((kp && kp.owned_audio) || (ap && ap.owned_audio));
  var price = (kp && kp.price != null) ? kp.price : (ap ? ap.price : null);
  detail.price = price;
  detail.currency = (kp && kp.price != null) ? kp.currency : (ap ? ap.currency : "");
  detail.price_text = (kp && kp.price != null) ? kp.price_text : (ap ? ap.price_text : "");
  detail.buttons = kp ? kp.buttons : (ap ? ap.buttons : []);
  detail.page = kp ? "kindle" : "audio";
  detail.buybox_html = (kp && kp.buybox_html) || (ap && ap.buybox_html) || "";
  if (kp && kp.audio_asin && audio && kp.audio_asin !== audio) detail.info += "NOTE: Kindle page links audiobook " + kp.audio_asin + " not " + audio + "; ";
  detail.info += (detail.signed_in === false ? "NOT signed in; " : "") + (detail.robot ? "robot check; " : "") +
                 "ku=" + detail.ku + " price=" + (price == null ? "none" : price) + " owned=" + detail.owned + " buttons=" + (detail.buttons || []).map(function (b) { return b.label; }).slice(0, 6).join("|");
  var ok = !detail.robot && detail.signed_in !== false;
  return { ok: ok, detail: detail };
}

function _rrWaitLoad(tabId, timeoutMs, requireNav) {
  return new Promise(function (resolve) {
    var done = false, sawLoading = !requireNav;
    function finish(v) { if (done) return; done = true; try { browser.tabs.onUpdated.removeListener(on); } catch (e) {} resolve(v); }
    function on(id, info) { if (id !== tabId) return; if (info.status === "loading") sawLoading = true; if (info.status === "complete" && sawLoading) setTimeout(function () { finish(true); }, 1200); }
    browser.tabs.onUpdated.addListener(on);
    setTimeout(function () { finish(false); }, timeoutMs);
  });
}

async function _rrExec(tabId, opts) {
  var code = "(" + RR_pageStep.toString() + ")(" + JSON.stringify(opts) + ")";
  var r = await browser.tabs.executeScript(tabId, { code: code, runAt: "document_idle" });
  return (r && r[0]) || { ok: false, info: "no result from page" };
}

async function RR_returnBorrow(host, kindleAsin) {
  // KU Central lists every borrow with its loan id + per-item csrf token; the
  // Return button POSTs JSON to /kindle-dbs/hz/ajax/returnItem and expects
  // KLU_RETURN_SUCCEEDED back.
  var base = "https://" + (host || "www.amazon.com.au");
  var html;
  try {
    var resp = await fetchTimeout(base + "/kindle-dbs/ku/ku-central", { credentials: "include" }, 45000);
    if (!resp.ok) return { ok: false, info: "KU Central http " + resp.status };
    html = await resp.text();
  } catch (e) { return { ok: false, info: "KU Central: " + String(e) }; }
  var doc = new DOMParser().parseFromString(html, "text/html");
  var span = doc.querySelector('#returnActionContainer-' + kindleAsin + ' [data-return-item-click], [data-return-item-click*="' + kindleAsin + '"]');
  if (!span) return { ok: false, info: "Kindle " + kindleAsin + " is not among the KU borrows (already returned, or the borrow did not happen)" };
  var data;
  try { data = JSON.parse(span.getAttribute("data-return-item-click")); } catch (e) { return { ok: false, info: "could not parse the return control" }; }
  if (!data.returnLoanID || !data.csrfToken) return { ok: false, info: "return control missing loan id / token" };
  var meta = doc.querySelector('meta[name="anti-csrftoken-a2z"]');
  var headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (meta && meta.getAttribute("content")) headers["anti-csrftoken-a2z"] = meta.getAttribute("content");
  try {
    var r2 = await fetchTimeout(base + "/kindle-dbs/hz/ajax/returnItem" + (data.refTag || "/ref=ku_mi"), { method: "POST", credentials: "include", headers: headers,
      body: JSON.stringify({ returnLoanID: data.returnLoanID, csrfToken: data.csrfToken }) }, 45000);
    var txt = await r2.text(); var j = null; try { j = JSON.parse(txt); } catch (e) {}
    var ok = !!(j && j.result === "KLU_RETURN_SUCCEEDED");
    return { ok: ok, info: ok ? "KLU_RETURN_SUCCEEDED" : ("return answered " + r2.status + ": " + txt.slice(0, 160)) };
  } catch (e) { return { ok: false, info: "return call failed: " + String(e) }; }
}

async function _rrHasLoan(host, kindleAsin, tries) {
  for (var i = 0; i < (tries || 1); i++) {
    try {
      var resp = await fetchTimeout("https://" + (host || "www.amazon.com.au") + "/kindle-dbs/ku/ku-central", { credentials: "include" }, 30000);
      if (resp.ok) { var html = await resp.text(); if (html.indexOf('returnActionContainer-' + kindleAsin) >= 0 || html.indexOf('"asin":"' + kindleAsin + '"') >= 0 || html.indexOf("&quot;asin&quot;:&quot;" + kindleAsin + "&quot;") >= 0) return true; }
    } catch (e) {}
    await new Promise(function (r) { setTimeout(r, 4000); });
  }
  return false;
}

async function _rrAjaxBorrow(host, ctx) {
  // Same shape as the Return button's call: JSON body + the page's CSRF token.
  var base = "https://" + (host || "www.amazon.com.au");
  var url = base + "/kindle-dbs/hz/ajax/borrow?ref=" + (ctx.borrowRef || "dbs_p_ebk_r00_pbcb_cvbru0");
  var body = { asin: ctx.asin, program: ctx.program || "KINDLE_UNLIMITED", channel: ctx.channel || "ALL_YOU_CAN_READ", borrowSubType: ctx.subType || "KINDLE_UNLIMITED", csrfToken: ctx.csrf };
  try {
    var r = await fetchTimeout(url, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json", "Accept": "application/json", "anti-csrftoken-a2z": ctx.csrf }, body: JSON.stringify(body) }, 30000);
    var txt = await r.text(); var j = null; try { j = JSON.parse(txt); } catch (e) {}
    var ok = r.ok && !!(j && (/SUCC/i.test(String(j.result || j.status || "")) || j.success === true));
    return { ok: ok, info: "ajax borrow " + r.status + ": " + txt.slice(0, 160) };
  } catch (e) { return { ok: false, info: "ajax borrow failed: " + String(e) }; }
}

async function RR_runBuy(cmd) {
  var job = cmd.job || {}; var phase = cmd.phase || "probe";
  logLine("buy[" + phase + "] " + (job.series || "?") + " #" + job.n + " " + (job.audio_asin || ""));
  if (phase === "probe") {
    var p = await RR_probe(job);
    await _rrReport(job.id, "probe", p.ok, p.detail);
    logLine("buy[probe] " + (p.ok ? "ok " : "FAIL ") + (p.detail.info || ""));
    return;
  }
  if (phase === "return") {
    var rk = job.kindle_asin;
    if (!rk && job.audio_asin) { try { var rp = RR_buyProbe(await _rrFetchDoc(_rrUrl(job.store_host || "www.amazon.com.au", job.audio_asin)), ""); rk = rp.kindle_asin; } catch (e) {} }
    var rr = rk ? await RR_returnBorrow(job.store_host || "www.amazon.com.au", rk) : { ok: false, info: "no Kindle ASIN known" };
    await _rrReport(job.id, "return", rr.ok, { info: rr.info });
    logLine("buy[return] " + (rr.ok ? "ok " : "FAIL ") + rr.info);
    return;
  }
  var dry = (phase === "dry");
  var rphase = dry ? "dry" : "execute";
  async function step(name, ok, info, extra) {
    var d = { step: name, info: info };
    if (extra) for (var k in extra) d[k] = extra[k];
    await _rrReport(job.id, rphase, ok, d);
    logLine("buy[" + phase + "] " + name + ": " + (ok ? "ok " : "FAIL ") + info);
  }
  // what it is doing right now, for the app's live Discord message (not a step)
  async function now(info) { try { await _rrReport(job.id, "progress", true, { info: info }); } catch (e) {} }
  var host = job.store_host || "www.amazon.com.au";
  var kindle = job.kindle_asin;
  if (!kindle && job.audio_asin) {
    try { var ap = RR_buyProbe(await _rrFetchDoc(_rrUrl(host, job.audio_asin)), _rrUrl(host, job.audio_asin)); kindle = ap.kindle_asin; } catch (e) {}
  }
  if (!kindle) { await step("borrow", false, "no Kindle edition ASIN known for this book", { final: true }); return; }
  var tab = null, borrowed = false;
  async function returnBorrow() {
    if (!borrowed && !job.ku_borrowed_by_us) { await step("return", true, "nothing to return (the borrow was not mine)"); return true; }
    await now("returning the Kindle Unlimited borrow");
    var ret = await RR_returnBorrow(host, kindle);
    await step("return", ret.ok, ret.info, dry ? { final: false } : null);
    return ret.ok;
  }
  try {
    // foreground tab: Firefox throttles scripts in background tabs and the KU
    // borrow is a page-script widget that simply never fired there
    // how the working tab is shown (app setting buy_tab_mode, sent in cfg):
    //   foreground - a normal tab in the current window, focused (known to work)
    //   window     - its own small window opened WITHOUT focus (doesn't steal the keyboard)
    //   background - an unfocused tab (Firefox throttles page scripts there; the KU widget did not fire)
    await now("opening the Kindle page for book " + job.n);
    var mode = (job.cfg && job.cfg.tab_mode) || "foreground";
    var madeWindow = null;
    if (mode === "window") {
      try {
        madeWindow = await browser.windows.create({ url: _rrUrl(host, kindle), focused: false, type: "normal", width: 1000, height: 820 });
        tab = (madeWindow.tabs && madeWindow.tabs[0]) || (await browser.tabs.query({ windowId: madeWindow.id }))[0];
      } catch (e) { madeWindow = null; }
    }
    if (!tab) tab = await browser.tabs.create({ url: _rrUrl(host, kindle), active: mode !== "background" });
    await _rrWaitLoad(tab.id, 40000, false);
    await new Promise(function (r) { setTimeout(r, 4000); });     // let Amazon's widgets bind
    var base = { audio_asin: job.audio_asin, price: job.price, cap: job.cap, cfg: job.cfg };
    // 1. borrow (free; returned at the end in both modes)
    await now("looking for the Kindle Unlimited borrow button");
    var b = await _rrExec(tab.id, Object.assign({ mode: "borrow" }, base));
    await step("borrow", b.ok, b.info, { clicked: !!b.clicked, found: b.found, html: b.html || "" });
    if (!b.ok) { if (dry) await _rrReport(job.id, "dry", false, { info: "stopped at borrow: " + b.info, final: true }); return; }
    if (b.found && (b.found.borrowed_already || b.found.in_library) && !b.clicked) borrowed = false;   // was already borrowed before me: leave it alone
    if (b.clicked) {
      await now("borrow pressed - waiting for Amazon to confirm it");
      // VERIFY the borrow: the widget normally redirects to the thank-you page;
      // failing that, the loan must show up in KU Central
      var navigated = await _rrWaitLoad(tab.id, 25000, true);
      var loaned = navigated ? true : await _rrHasLoan(host, kindle, 3);
      if (!loaned && b.kuctx && b.kuctx.csrf) {
        // fallback: the widget's own AJAX borrow, with the context it embeds
        var fb = await _rrAjaxBorrow(host, b.kuctx);
        await step("borrow-ajax", fb.ok, fb.info);
        loaned = fb.ok || await _rrHasLoan(host, kindle, 2);
        if (loaned && b.kuctx.typ) { try { await browser.tabs.update(tab.id, { url: b.kuctx.typ + "&asin=" + kindle }); await _rrWaitLoad(tab.id, 30000, true); } catch (e) {} }
      }
      await step("borrow-verify", loaned, loaned ? ("borrowed" + (navigated ? " (page moved on)" : " (seen in KU Central)")) : "the click had no effect: no navigation and no loan in KU Central");
      if (!loaned) { if (dry) await _rrReport(job.id, "dry", false, { info: "stopped: the KU borrow did not take", final: true }); return; }
      borrowed = true;
    }
    // 2. find the audiobook offer: where we landed, then the Kindle page, then the audiobook page
    var offer = null, where = "";
    var places = [["page after the borrow", null], ["Kindle page", _rrUrl(host, kindle)]];
    if (b.kuctx && b.kuctx.typ) places.splice(1, 0, ["thank-you page", b.kuctx.typ + "&asin=" + kindle]);
    if (job.audio_asin) places.push(["audiobook page", _rrUrl(host, job.audio_asin)]);
    for (var i = 0; i < places.length && !offer; i++) {
      if (places[i][1]) { await browser.tabs.update(tab.id, { url: places[i][1] }); await _rrWaitLoad(tab.id, 40000, true); }
      await now("looking for the $" + job.price + " audiobook offer on the " + places[i][0]);
      var o = await _rrExec(tab.id, Object.assign({ mode: "offer", commit: false }, base));
      if (!o.ok && o.expanded) {        // a collapsed buying option was opened: look again
        await new Promise(function (r) { setTimeout(r, 1800); });
        o = await _rrExec(tab.id, Object.assign({ mode: "offer", commit: false, no_expand: true }, base));
      }
      // a miss here is information, not a failure - the search moves to the next page
      await step("offer on " + places[i][0], true, o.ok ? o.info : ("not here: " + o.info), { found: o.found, html: o.html || "" });
      if (o.ok) { offer = o; where = places[i][0]; }
    }
    if (!offer) {
      if (dry) await _rrReport(job.id, "dry", false, { info: "no audiobook offer found after borrowing (checked the landing page, the Kindle page and the audiobook page)" + (borrowed ? "; the KU borrow is left in place for the next check" : ""), final: true, borrowed_by_us: borrowed });
      else { await returnBorrow(); await step("offer", false, "no audiobook offer found after borrowing - nothing bought", { final: true }); }
      return;
    }
    if (dry) {
      // keep the borrow: the next check (or the real run) then skips straight to the offer
      await _rrReport(job.id, "dry", true, { info: "DRY RUN OK: would click " + offer.info.replace(/^offer: /, "") + " on the " + where + (borrowed ? "; KU borrow kept for the real run" : ""), final: true, borrowed_by_us: borrowed });
      try { if (madeWindow) await browser.windows.remove(madeWindow.id); else await browser.tabs.remove(tab.id); } catch (e) {}
      return;
    }
    // 3. buy (execute only): click the one offer button we just verified
    await now("pressing Buy on the $" + job.price + " audiobook offer");
    var c1 = await _rrExec(tab.id, Object.assign({ mode: "offer", commit: true, no_expand: true }, base));
    await step("buy-click", c1.ok, c1.info, { clicked: !!c1.clicked });
    if (!c1.ok) { await returnBorrow(); return; }
    await _rrWaitLoad(tab.id, 60000, true);
    // a review page may sit between "Buy Now" and the confirmation; checkout
    // pages render late, so look several times before deciding
    var c = null, rv = null, pages = 0;
    while (pages++ < 5) {                         // payment page -> review page -> confirmation
      await now("reading the checkout page");
      c = await _rrExec(tab.id, { mode: "confirm", price: job.price, title: job.title, audio_asin: job.audio_asin, cfg: job.cfg });
      if (c.ok) break;
      if (c.blocked) { rv = c; break; }
      rv = null;
      for (var k = 0; k < 4; k++) {               // the page may still be rendering
        rv = await _rrExec(tab.id, Object.assign({ mode: "checkout", commit: true }, base));
        if (rv.ok || rv.blocked) break;
        await new Promise(function (r) { setTimeout(r, 3000); });
      }
      if (rv && rv.blocked) break;
      if (rv && rv.clicked) { await step(rv.stage === "payment" ? "payment" : "review", true, rv.info, { clicked: true, found: rv.found }); await _rrWaitLoad(tab.id, 60000, true); continue; }
      break;
    }
    if (rv && rv.blocked) {
      // nothing was processed (Amazon said so): plain failure, not "uncertain"; the borrow stays for the retry
      await step("checkout", false, rv.info + " - try again later", { clicked: false, found: rv.found, final: true });
      return;
    }
    if (!c.ok && rv && !rv.clicked) await step("review", false, rv.info + " | page: " + ((rv.found && rv.found.url) || "") + " | " + ((rv.found && rv.found.excerpt) || "").slice(0, 200), { found: rv.found, html: rv.html || "" });
    if (!c.ok) c.info = c.info + " | page: " + ((c.found && c.found.url) || "") + " | " + ((c.found && c.found.excerpt) || "").slice(0, 200);
    await step("confirm", c.ok, c.info, { clicked: true, found: c.found, html: c.html || "" });
    if (!c.ok) { logLine("buy[execute] could not confirm: " + c.info); return; }   // tab left open; app marks uncertain
    // 4. return the KU borrow
    var ok = await returnBorrow();
    if (ok) { try { if (madeWindow) await browser.windows.remove(madeWindow.id); else await browser.tabs.remove(tab.id); } catch (e) {} }
  } catch (e) {
    await step("borrow", false, "error: " + String(e), { clicked: false, final: true });
    logLine("buy[" + phase + "] error: " + String(e));
  }
}

if (typeof self !== "undefined") { self.RR_buyProbe = RR_buyProbe; self.RR_pageStep = RR_pageStep; }
