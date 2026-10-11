// The Royal Road collector's parsers, against the markup Royal Road actually
// serves (captured 7 Oct 2026: window.chapters on the fiction page, .comment /
// .media-heading / .comment-body in commentsLegacy, chapter-content bodies).
//
//   node release-radar-firefox/test/test_rrnotes.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");
const { makeDoc } = require("./minidom");

process.exitCode = 1;
const HERE = __dirname, CACHE = path.join(HERE, ".fixture-cache");
let fails = 0;
function chk(name, ok, extra) { if (ok) return; fails++; console.log("  FAIL " + name + (extra ? "  " + extra : "")); }
function havePython() { try { execFileSync("python", ["-c", "import html5lib"], { stdio: "ignore" }); return true; } catch (e) { return false; } }
if (!havePython()) { console.log("test_rrnotes: python+html5lib not available — skipping"); process.exit(0); }
function docFor(html, key) {
  fs.mkdirSync(CACHE, { recursive: true });
  const src = path.join(CACHE, key + ".html"), out = path.join(CACHE, key + ".json");
  fs.writeFileSync(src, html);
  execFileSync("python", [path.join(HERE, "html2json.py"), src, out], { stdio: "inherit" });
  return makeDoc(JSON.parse(fs.readFileSync(out, "utf8")));
}

// load background.js with the same stubs the lock harness uses
function stub() { const fn = function () { return Promise.resolve({}); }; return new Proxy(fn, { get(t, k) { if (k === "then") return undefined; if (k === "addListener") return () => {}; return stub(); }, apply() { return Promise.resolve({}); } }); }
const sandbox = { console, URL, AbortController, setTimeout, clearTimeout, Date, Math, JSON, Promise, setInterval: () => 0, clearInterval: () => 0,
  browser: stub(), fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(""), json: () => Promise.resolve({}) }), DOMParser: function () {} };
sandbox.self = sandbox; sandbox.window = sandbox;
vm.createContext(sandbox);
for (const f of ["extract.js", "buy.js", "background.js"]) vm.runInContext(fs.readFileSync(path.join(HERE, "..", f), "utf8"), sandbox, { filename: f });

console.log("Royal Road collector parsers:");
// fiction page: chapter list + author
const fiction = `<html><body><div class="fic-header"><h4><span class="white">by</span> <a href="/profile/97247" class="font-white">RinoZ</a></h4></div>
<script>window.chapters = [{"id":3476050,"title":"MERCH IS LIVE","url":"/fiction/22518/chrysalis/chapter/3476050/merch-is-live","date":"2026-06-01T02:17:41Z","order":500},
{"id":3500000,"title":"Chapter 1800 - Onward","url":"/fiction/22518/chrysalis/chapter/3500000/chapter-1800-onward","date":"2026-07-01T01:00:00Z","order":501},
{"id":3600000,"title":"Book 10 release + audiobook update","url":"/fiction/22518/chrysalis/chapter/3600000/book-10","date":"2026-09-20T01:00:00Z","order":502},
{"id":4071521,"title":"Chapter 1876 - New Beginnings","url":"/fiction/22518/chrysalis/chapter/4071521/chapter-1876-new-beginnings","date":"2026-10-06T01:00:13Z","order":530},
{"id":4080738,"title":"Chapter 1877 - Rip and Tear","url":"/fiction/22518/chrysalis/chapter/4080738/chapter-1877-rip-and-tear","date":"2026-10-07T01:00:14Z","order":531}];
window.fictionCover = "x";</script></body></html>`;
const chapters = sandbox.RR_rrChapters(fiction);
chk("chapter list parsed from window.chapters", chapters.length === 5 && chapters[4].id === 4080738, JSON.stringify(chapters.length));
chk("author id and name parsed", JSON.stringify(sandbox.RR_rrAuthor(fiction)) === JSON.stringify({ id: "97247", name: "RinoZ" }), JSON.stringify(sandbox.RR_rrAuthor(fiction)));
const pick = sandbox.RR_rrPick(chapters);
chk("announcement chapters picked by title, newest story chapters too, no duplicates",
    pick.map(p => p.id).join(",") === "3476050,3600000,3500000,4071521,4080738" && pick[1].announcement === true && pick[4].announcement === false,
    JSON.stringify(pick.map(p => [p.id, p.announcement])));
chk("a story chapter whose title mentions a book isn't an announcement", sandbox.RR_rrIsAnnouncement("Chapter 1812 - Book 10 of the Codex") === false);

// chapter page: announcement body + author's note
const chapter = docFor(`<html><body><div class="portlet author-note-portlet"><div class="portlet-body author-note"><p>Audiobooks 8 and 9 are with the narrator - November, then back to back.</p></div></div>
<div class="chapter-inner chapter-content"><p>Book 10 is out on Amazon today!</p><p>Thank you all.</p></div></body></html>`, "rr-chapter");
const ann = sandbox.RR_rrNotesFromChapter(chapter, { title: "Book 10 release + audiobook update", date: "2026-09-20T01:00:00Z", announcement: true });
chk("announcement body captured once", ann.filter(i => i.kind === "announcement").length === 1 && /Book 10 is out on Amazon/.test(ann[0].text), JSON.stringify(ann));
chk("author's note captured once (portlet, not its inner box twice)", ann.filter(i => i.kind === "note").length === 1 && /November, then back to back/.test(ann.find(i => i.kind === "note").text), JSON.stringify(ann));
const story = sandbox.RR_rrNotesFromChapter(chapter, { title: "Chapter 1877", date: "x", announcement: false });
chk("a story chapter contributes only its author's note", story.length === 1 && story[0].kind === "note");

// comments page: only the author's own replies
const comments = docFor(`<html><body><div class="comment"><div class="media"><div class="media-body"><h4 class="media-heading"><span class="name"><strong><a href="/profile/88913">MInsanity</a></strong></span>
<a href="/fiction/chapter/4080738?comment=1"><small><time datetime="2026-10-07T01:03:57.0000000+00:00">4 hours</time> ago</small></a></h4><div class="comment-body"><p>When is book 10 out?</p></div></div></div></div>
<div class="comment"><div class="media"><div class="media-body"><h4 class="media-heading"><span class="name"><strong><a href="/profile/97247">RinoZ</a></strong></span>
<a href="/fiction/chapter/4080738?comment=2"><small><time datetime="2026-10-07T03:00:00.0000000+00:00">2 hours</time> ago</small></a></h4><div class="comment-body"><p>November 11th, and the audiobooks for 8-10 come right after each other.</p></div></div></div></div></body></html>`, "rr-comments");
const replies = sandbox.RR_rrRepliesFromComments(comments, "97247", { title: "Chapter 1877 - Rip and Tear", date: "2026-10-07T01:00:14Z" });
chk("only the author's reply is kept, with its own timestamp", replies.length === 1 && /November 11th/.test(replies[0].text) && replies[0].date.startsWith("2026-10-07T03:00") && replies[0].kind === "reply", JSON.stringify(replies));
chk("another author id matches nothing", sandbox.RR_rrRepliesFromComments(comments, "1", { title: "x", date: "y" }).length === 0);

console.log(fails ? "\n" + fails + " FAILURE(S)" : "\nall Royal Road collector checks correct");
process.exit(fails ? 1 : 0);
