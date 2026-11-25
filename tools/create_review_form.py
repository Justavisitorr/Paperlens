"""Create an offline human review form from a label-blind audit packet.

This creates a review instrument, not human judgments. Unreviewed nodes are
omitted from the exported judgments, as audit_reports.summarize counts them by
the difference from the packet's full node census.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def review_payload(packet: dict) -> dict:
    """Whitelist visible fields so producer support labels cannot leak in."""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", packet["report_sha256"]):
        raise ValueError("Expected a SHA-256 report snapshot")
    nodes = []
    for node in packet["nodes"]:
        visible = {
            key: node[key] for key in ("id", "kind", "title", "summary", "explanation", "origin")
        }
        visible["sources"] = [
            {
                key: source.get(key)
                for key in (
                    "evidence_id",
                    "block_id",
                    "page",
                    "page_label",
                    "section",
                    "quote",
                    "block_text",
                )
            }
            for source in node["sources"]
        ]
        nodes.append(visible)
    return {
        "paper_id": packet["paper_id"],
        "report_sha256": packet["report_sha256"],
        "pdf_sha256": packet["pdf_sha256"],
        "nodes": nodes,
        "source_blocks": [
            {key: block.get(key) for key in ("id", "page", "section", "text")}
            for block in packet["source_blocks"]
        ],
    }


def render_form(packet: dict) -> str:
    data = json.dumps(review_payload(packet), ensure_ascii=False)
    for character, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        data = data.replace(character, escaped)
    return TEMPLATE.replace("__PACKET_JSON__", data)


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>论文节点人工复核</title>
<style>
:root{color-scheme:light;font-family:system-ui,"Microsoft YaHei",sans-serif;color:#172b3a;background:#f2f5f7}
*{box-sizing:border-box}body{margin:0;line-height:1.65}main{max-width:1050px;margin:auto;padding:28px 20px 80px}
h1{font-size:28px;line-height:1.3}h2{font-size:21px;margin:0 0 12px}h3{font-size:16px;margin:12px 0 6px}
p{margin:8px 0}button,input,select,textarea{font:inherit}button,select,input,textarea{border:1px solid #94a6b4;border-radius:6px;padding:9px;background:white;color:inherit}
button{cursor:pointer;background:#174f72;color:white;border-color:#174f72}button:hover{background:#123d58}
:focus-visible{outline:3px solid #ea8d21;outline-offset:3px}textarea{width:100%;min-height:95px;resize:vertical}
.notice{background:#e7f0f6;border-left:4px solid #326b91;padding:12px 16px}.muted{color:#496273;font-size:14px}
.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:18px 0}.toolbar label{display:flex;align-items:center;gap:8px}
.node{background:white;border:1px solid #cad5dd;border-radius:12px;padding:24px;margin:20px 0;scroll-margin-top:20px}
.node.completed{border-left:5px solid #287658}.node.error{border:2px solid #b32c30}.text{white-space:pre-wrap;overflow-wrap:anywhere}
.source{padding:14px;margin:12px 0;border:1px solid #d3dde4;border-radius:6px;background:#f8fafb}
blockquote{margin:8px 0;padding-left:12px;border-left:3px solid #8daabb;white-space:pre-wrap;overflow-wrap:anywhere}
summary{cursor:pointer;color:#174f72}summary:focus-visible{outline-offset:1px}.full-block{padding:10px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.check{display:flex;gap:9px;align-items:flex-start;margin-top:12px}.check input{width:19px;height:19px;flex-shrink:0;margin-top:4px}
.judgment{border-top:1px solid #d3dde4;margin-top:18px;padding-top:14px}.judgment>label{display:block;margin-top:10px;font-weight:600}
.extra{max-width:100%}.extra select{max-width:100%;width:100%;margin:8px 0}.extra button{margin-bottom:8px}
#status{min-height:26px;font-weight:600}#status[data-error=true]{color:#b32c30}.hash{font-family:monospace;overflow-wrap:anywhere}
.footer{position:sticky;bottom:0;padding:12px 0;background:#f2f5f7;border-top:1px solid #bdcbd5;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
@media(max-width:600px){main{padding:16px 12px 35px}.node{padding:16px}.toolbar label{display:block}h1{font-size:24px}.footer{position:static}}
</style></head><body><main>
<h1>论文节点人工复核</h1>
<p id="paper" class="muted"></p>
<div class="notice"><strong>这是一份待填写的复核表，不代表已完成人类评测。</strong>
<p>请同时判断节点标题、摘要和通俗解释；一个实质性断言超出证据，就不应选择“支持”。生产模型的判断已隐藏。</p>
<p>“支持”：所有实质性断言均有依据；“部分”：部分内容有依据，但有缺失条件或超出证据的表述；“无支持”：读过的来源不能支持该节点；“矛盾”：来源明确反驳关键断言。暂未判断请保留“未审”。</p>
<p>页码为 PDF 物理页码。可展开完整提取块，也可添加论文其他块。提取文本可能错列或丢失表格关系，请对照原始 PDF 核查；本页未内嵌原 PDF。</p>
<p>仅勾选你实际读过的来源。草稿尽量保存在当前浏览器，移动文件或更换浏览器可能丢失，请及时导出。导出的 JSON 只含已审节点，放到对应 packet 文件旁可进行汇总。</p></div>
<div class="toolbar"><label for="reviewer">复核者（选填）<input id="reviewer" autocomplete="off" maxlength="120"></label>
<label for="filter">显示节点<select id="filter"><option value="all">全部</option><option value="pending">未审</option><option value="done">已判</option></select></label>
<span id="progress" aria-live="polite"></span></div>
<p class="muted">报告快照 SHA-256：<span id="hash" class="hash"></span></p>
<div id="nodes"></div>
<div class="footer"><button id="export" type="button">导出人工复核 JSON</button><span id="status" role="status" aria-live="polite"></span></div>
<noscript>请启用浏览器 JavaScript 后填写；本页不连接网络。</noscript>
</main><script id="packet" type="application/json">__PACKET_JSON__</script>
<script>
"use strict";
(() => {
 const packet=JSON.parse(document.getElementById("packet").textContent);
 const storageKey="paperlens-human-review-v1:"+packet.paper_id+":"+packet.report_sha256;
 const choices=[['not_reviewed','未审'],['supported','支持'],['partial','部分'],['unsupported','无支持'],['contradicted','矛盾']];
 const blocks=new Map(packet.source_blocks.map(block=>[block.id,block]));
 const cards=[];
 let storageAvailable=true;
 const el=(tag,text,className)=>{const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;};
 const status=(text,error=false)=>{const item=document.getElementById("status");item.textContent=text;item.dataset.error=String(error);};
 document.getElementById("paper").textContent=packet.paper_id+" · "+packet.nodes.length+" 个完整节点";
 document.getElementById("hash").textContent=packet.report_sha256;
 const pageLabel=block=>"PDF 第 "+block.page+" 页"+(block.section?" · "+block.section:"")+" · "+block.id;
 function addBlock(card,block,sources=[],restored=false){
   if(card.checks.has(block.id))return;
   const source=el("div",undefined,"source");
   source.append(el("p",pageLabel(block),"muted"));
   for(const quote of sources){
     source.append(el("blockquote",quote.quote));
     if(quote.page_label)source.append(el("p","论文页标："+quote.page_label,"muted"));
   }
   const details=el("details");
   details.append(el("summary","展开完整提取块"),el("div",block.text,"full-block"));
   const label=el("label",undefined,"check");
   const check=el("input");check.type="checkbox";check.dataset.blockId=block.id;
   label.append(check,el("span","我已阅读这份来源（"+block.id+"）"));
   source.append(details,label);card.sources.append(source);card.checks.set(block.id,check);
   if(!restored && sources.length===0){details.open=true;check.focus();}
 }
 function readState(){return {report_sha256:packet.report_sha256,reviewer:document.getElementById("reviewer").value,
   nodes:cards.map(card=>({node_id:card.node.id,verdict:card.verdict.value,rationale:card.rationale.value,
     visible_block_ids:[...card.checks.keys()],checked_block_ids:[...card.checks].filter(([,check])=>check.checked).map(([id])=>id)}))};}
 function update(){
   const done=cards.filter(card=>card.verdict.value!=="not_reviewed").length;
   document.getElementById("progress").textContent="已判 "+done+" / "+cards.length+"；导出前将校验理由与已读来源";
   const filter=document.getElementById("filter").value;
   for(const card of cards){const reviewed=card.verdict.value!=="not_reviewed";card.element.classList.toggle("completed",reviewed);card.element.hidden=(filter==="pending"&&reviewed)||(filter==="done"&&!reviewed);}
 }
 function save(){
   update();
   try{localStorage.setItem(storageKey,JSON.stringify(readState()));status("草稿已保存到当前浏览器");}
   catch{storageAvailable=false;status("浏览器无法保存草稿，请在关闭前导出。",true);}
 }
 for(const [index,node] of packet.nodes.entries()){
   const section=el("section",undefined,"node");
   const heading=el("h2",String(index+1)+". "+node.title);heading.id="node-title-"+index;section.setAttribute("aria-labelledby",heading.id);
   section.append(heading,el("p",node.id+" · "+node.kind+" · "+node.origin,"muted"),el("h3","摘要"),el("p",node.summary,"text"),el("h3","通俗解释"),el("p",node.explanation||"（无）","text"),el("h3","引用来源"));
   const sources=el("div");section.append(sources);
   const card={node,element:section,sources,checks:new Map()};cards.push(card);
   const grouped=new Map();for(const source of node.sources){if(!grouped.has(source.block_id))grouped.set(source.block_id,[]);grouped.get(source.block_id).push(source);}
   for(const [id,quotes] of grouped){const block=blocks.get(id);if(block)addBlock(card,block,quotes,true);}
   if(!grouped.size)sources.append(el("p","节点未附来源。请展开下方论文文本，阅读后添加适合判断的块。","muted"));
   const extra=el("details",undefined,"extra");extra.append(el("summary","查阅并添加论文的其他文本块"));
   const pick=el("select");pick.setAttribute("aria-label","选择其他来源块："+node.id);
   for(const block of packet.source_blocks){const option=el("option",pageLabel(block)+" · "+block.text.slice(0,65));option.value=block.id;pick.append(option);}
   const add=el("button","添加并阅读所选块");add.type="button";add.addEventListener("click",()=>{const block=blocks.get(pick.value);if(block){addBlock(card,block);save();}});
   extra.append(pick,add);section.append(extra);
   const judgment=el("div",undefined,"judgment");
   const verdict=el("select");verdict.id="verdict-"+index;verdict.className="verdict";
   for(const [value,title] of choices){const option=el("option",title);option.value=value;verdict.append(option);}
   const verdictLabel=el("label","判断");verdictLabel.htmlFor=verdict.id;
   const rationale=el("textarea");rationale.id="rationale-"+index;rationale.className="rationale";rationale.placeholder="说明哪些断言获得支持，哪些条件、数字或推论存在问题；可写出对应页码。";
   const rationaleLabel=el("label","判断理由（已判节点必填）");rationaleLabel.htmlFor=rationale.id;
   judgment.append(verdictLabel,verdict,rationaleLabel,rationale);section.append(judgment);card.verdict=verdict;card.rationale=rationale;
   document.getElementById("nodes").append(section);
 }
 try{
   const draft=JSON.parse(localStorage.getItem(storageKey)||"null");
   if(draft&&draft.report_sha256===packet.report_sha256){
     document.getElementById("reviewer").value=typeof draft.reviewer==="string"?draft.reviewer:"";
     const saved=new Map((Array.isArray(draft.nodes)?draft.nodes:[]).map(node=>[node.node_id,node]));
     for(const card of cards){const node=saved.get(card.node.id);if(!node)continue;
       if(choices.some(([value])=>value===node.verdict))card.verdict.value=node.verdict;
       card.rationale.value=typeof node.rationale==="string"?node.rationale:"";
       for(const id of Array.isArray(node.visible_block_ids)?node.visible_block_ids:[]){if(blocks.has(id))addBlock(card,blocks.get(id),[],true);}
       const checked=new Set(Array.isArray(node.checked_block_ids)?node.checked_block_ids:[]);
       for(const [id,input] of card.checks)input.checked=checked.has(id);
     }
     status("已恢复这份报告的本地草稿");
   }
 }catch{storageAvailable=false;status("无法恢复浏览器草稿；可继续填写并导出。",true);}
 document.getElementById("nodes").addEventListener("input",save);
 document.getElementById("nodes").addEventListener("change",save);
 document.getElementById("reviewer").addEventListener("input",save);
 document.getElementById("filter").addEventListener("change",update);
 document.getElementById("export").addEventListener("click",()=>{
   for(const card of cards)card.element.classList.remove("error");
   const state=readState();const judgments=[];
   for(const item of state.nodes){
     if(item.verdict==="not_reviewed")continue;
     if(!item.rationale.trim()||!item.checked_block_ids.length){
       const card=cards.find(candidate=>candidate.node.id===item.node_id);
       document.getElementById("filter").value="all";update();card.element.classList.add("error");card.element.scrollIntoView({block:"center"});
       if(!item.rationale.trim())card.rationale.focus();else card.element.querySelector("input[type=checkbox],summary").focus();
       status("无法导出：节点 "+item.node_id+" 需要填写理由，并勾选至少一份实际读过的来源。",true);return;
     }
     judgments.push({node_id:item.node_id,verdict:item.verdict,rationale:item.rationale.trim(),checked_block_ids:item.checked_block_ids});
   }
   if(!judgments.length){status("尚未填写判断。至少复核一个节点后再导出；未审节点不会被当作已审。",true);return;}
   const review={schema_version:1,paper_id:packet.paper_id,report_sha256:packet.report_sha256,reviewer_type:"human",reviewer_name:state.reviewer.trim(),reviewed_at:new Date().toISOString(),
     unit:"one entire node (title, summary and explanation)",method:"Human-entered judgments; checked_block_ids explicitly selected by the reviewer.",judgments};
   const url=URL.createObjectURL(new Blob([JSON.stringify(review,null,2)+"\n"],{type:"application/json;charset=utf-8"}));
   const link=el("a");link.href=url;link.download=packet.paper_id.replace(/[^a-zA-Z0-9_-]/g,"_")+".review.json";document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
   status("已导出 "+judgments.length+" 个已审节点；其余 "+(cards.length-judgments.length)+" 个仍为未审。"+(storageAvailable?"":" 请保留下载文件。"));
 });
 update();document.documentElement.dataset.ready="true";
})();
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding="utf-8-sig"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_form(packet), encoding="utf-8")
    print(f"Created an unfilled human review form: {args.output}")


if __name__ == "__main__":
    main()
