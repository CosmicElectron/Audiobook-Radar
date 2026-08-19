# Parse saved Amazon HTML with html5lib (spec-compliant, same tree DOMParser builds)
# and dump a JSON tree: element = [tag, {attrs}, [children]], text = "string".
# Comments dropped. <template> children are dropped to mirror DOMParser
# (template contents live in an inert DocumentFragment, invisible to
# querySelectorAll / TreeWalker on the document).
import sys, json, html5lib, time
from xml.dom import Node

src, dst = sys.argv[1], sys.argv[2]
t0 = time.time()
with open(src, "rb") as f:
    data = f.read()
doc = html5lib.parse(data, treebuilder="dom", transport_encoding="utf-8")

def conv(node):
    if node.nodeType == Node.TEXT_NODE:
        return node.data
    if node.nodeType == Node.ELEMENT_NODE:
        tag = node.tagName.lower()
        attrs = {}
        if node.attributes:
            for i in range(node.attributes.length):
                a = node.attributes.item(i)
                attrs[a.name] = a.value
        kids = []
        if tag != "template":
            for c in node.childNodes:
                r = conv(c)
                if r is not None:
                    kids.append(r)
        return [tag, attrs, kids]
    return None

root = None
for c in doc.childNodes:
    if c.nodeType == Node.ELEMENT_NODE:
        root = conv(c)
title = ""
for t in doc.getElementsByTagName("title"):
    title = "".join(x.data for x in t.childNodes if x.nodeType == Node.TEXT_NODE)
    break
with open(dst, "w", encoding="utf-8") as f:
    json.dump({"title": title, "root": root, "htmlLen": len(data)}, f)
print("ok", src, "->", dst, "%.1fs" % (time.time() - t0))
