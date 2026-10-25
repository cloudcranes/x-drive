"""Execute current UI functions in Node with an offline DOM and deferred HTTP stubs.

No copied implementation, application imports, server, browser or real network.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "webui.html"

HARNESS = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert/strict");
const src = fs.readFileSync(process.argv[1], "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
class Node {
  constructor(tag="div", cls="", text="") {
    this.tagName=tag.toUpperCase(); this.className=cls || ""; this._text=String(text ?? "");
    this.children=[]; this.parentNode=null; this.style={}; this.dataset={}; this.listeners={};
    this.disabled=false; this.hidden=false; this.scrollTop=0; this.value="";
    this.classList={
      add: (...names)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...names])].join(" ")},
      remove: (...names)=>{this.className=this.className.split(/\s+/).filter(n=>!names.includes(n)).join(" ")},
      contains: name=>this.className.split(/\s+/).includes(name),
      toggle: (name,on)=>{if(on===undefined)on=!this.classList.contains(name); this.classList[on?"add":"remove"](name);},
    };
  }
  set textContent(v) {this._text=String(v ?? ""); for(const c of this.children)c.parentNode=null; this.children=[];}
  get textContent() {return this._text+this.children.map(c=>c.textContent).join("");}
  get firstChild() {return this.children[0] || null;}
  get firstElementChild() {return this.firstChild;}
  get nextSibling() {return this.parentNode?.children[this.parentNode.children.indexOf(this)+1] || null;}
  get nextElementSibling() {return this.nextSibling;}
  append(...items) {for(const item of items)this.insertBefore(item,null);}
  insertBefore(item,anchor) {if(item===anchor)return;item.remove();const i=anchor?this.children.indexOf(anchor):this.children.length;assert(i>=0);this.children.splice(i,0,item);item.parentNode=this;}
  remove() {if(this.parentNode){const a=this.parentNode.children;a.splice(a.indexOf(this),1);this.parentNode=null;}}
  replaceWith(item) {const p=this.parentNode;if(p){p.insertBefore(item,this);this.remove();}}
  setAttribute(k,v) {this[k]=v;}
  addEventListener(type,fn) {(this.listeners[type]??=[]).push(fn);}
  matches(selector) {return selector.startsWith(".")?this.classList.contains(selector.slice(1)):this.tagName===selector.toUpperCase();}
  querySelectorAll(selector) {const sels=selector.split(",").map(s=>s.trim());return this.children.flatMap(c=>[...(sels.some(s=>c.matches(s))?[c]:[]),...c.querySelectorAll(selector)]);}
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  closest(selector) {return this.matches(selector)?this:this.parentNode?.closest(selector);}
  async dispatch(type,event={}) {event.target??=this;event.stopPropagation??=(()=>{event.stopped=true;});
    for(const f of this.listeners[type]||[])await f(event);if(this.parentNode&&!event.stopped)await this.parentNode.dispatch(type,event);}
}
const els=new Map(), flashes=[], opened=[];
const get=id=>{if(!els.has(id))els.set(id,new Node());return els.get(id);};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,statusText:"fixture",text:async()=>JSON.stringify(data)});
// 最小 document：弹层要按 id 挂载/移除，body 要能记滚动锁
// getElementById **不能**自动创建 —— 找不到必须返回 null，否则「弹层已关闭」
// 这类断言永远为假。
const bodyNode=new Node("body");
const docListeners={};
function findById(root,id){
  if(root.id===id) return root;
  for(const c of root.children||[]){const hit=findById(c,id);if(hit) return hit;}
  return null;
}
const document={
  body:bodyNode,
  getElementById:(id)=>findById(bodyNode,id),
  createElement:(tag)=>new Node(tag),
  addEventListener:(type,fn)=>{(docListeners[type]??=[]).push(fn);},
  removeEventListener:(type,fn)=>{const l=docListeners[type]||[];const i=l.indexOf(fn);if(i>=0)l.splice(i,1);},
  dispatchEvent:(ev)=>{for(const f of docListeners[ev.type]||[])f(ev);return true;},
};
const ctx={console,assert,Node,els,flashes,opened,get,deferred,response,document,docListeners,
  AbortController,setTimeout,clearTimeout,
  $:get,el:(...args)=>new Node(...args),BASE:"",authInit:x=>x,
  gb:n=>String(n),fmt:n=>String(n||""),queueFlash:(message,error)=>flashes.push({message,error}),
  localStorage:{getItem:()=>"1",setItem(){}},confirm:()=>true,
  window:{open:(...args)=>opened.push(args)},applyStorageScope(){},
  fetch:async()=>{throw new Error("unexpected fixture request")},load:()=>{},
  askRename:()=>{ctx.renamed=(ctx.renamed||0)+1},askDelete(){},applyFilter(){},
  jobTimeline:j=>new Node("div","tly",JSON.stringify(j.events || j.error || "")),
};
vm.createContext(ctx);
function part(start,end) {const a=src.indexOf(start),b=src.indexOf(end,a+start.length);assert(a>=0&&b>a,start);vm.runInContext(src.slice(a,b),ctx);}
part("async function subsApi(","async function renderSubs(");
part("async function metaApi(","// 体检区：");
part("// ── 卡片\n","// 删除归档：");
part("const mounted = new Map();","function applyFilter()");
part("const STAGE_TEXT =","// ── 图片灯箱");
part("const XL_PANEL_OPEN =","// 队列增量渲染：");
part("const QUEUE_NODES =","// ─────────────── 封面卡片");
part("const settingsDirty =","function openSettings()");
part("function settingsTabOf(","// 存储分区按");
part("function settingsRow(",'$("settings-open").addEventListener');
part("const RES_SORTS =","async function searchResources(");
part("function renderCandidates(","// ── 顶栏类型切换");
part("let lightbox = null;","function showDetail(");
ctx.resState={sort:"rec"};
'''


def run_js(body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for offline UI behavior tests")
    proc = subprocess.run(
        [node, "-e", HARNESS + "\n(async()=>{\n" +
         "await vm.runInContext('(async()=>{' + " + repr(body) +
         " + '\\n})()',ctx);\n})().catch(e=>{console.error(e);process.exitCode=1});", str(PAGE)],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_reused_job_and_panel_nodes_receive_latest_payload_and_timeline():
    run_js(r'''
      const first={id:"j",stage:"downloading",code:"ABC",percent:10,speed:1048576,
        error:"old",events:[{kind:"stage",detail:"old"}]};
      let frame=first;
      fetch=async url=>response(url.endsWith("/api/jobs")?{jobs:[frame]}:{tasks:[
        {id:"xl",name:"fixture",phase:"PHASE_TYPE_RUNNING",percent:frame.percent,speed:frame.speed}]});
      await loadJobs();
      const card=QUEUE_NODES.get("job:j").node, panel=QUEUE_NODES.get("xl:xl").node;
      await card.querySelector(".jtl").dispatch("click");
      await loadJobs(); // first poll after opening can change the structural signature
      const stable=QUEUE_NODES.get("job:j").node;
      frame={...first,percent:80,speed:8388608,error:"new",events:[{kind:"stage",detail:"new"}]};
      await loadJobs();
      assert.equal(QUEUE_NODES.get("job:j").node,stable);
      assert.equal(QUEUE_NODES.get("xl:xl").node,panel);
      assert.equal(stable.querySelector(".jbar").firstChild.style.width,"80%");
      assert.match(stable.querySelector(".jmeta").textContent,/8.0MB\/s/);
      assert.match(stable.querySelector(".jmeta").textContent,/new/);
      assert.match(stable.querySelector(".tly").textContent,/new/);
      assert.equal(panel.querySelector(".jbar").firstChild.style.width,"80%");
      assert.match(panel.querySelector(".jmeta").textContent,/8.0MB\/s/);
      frame={...frame,cleanup_pending:[{target:"inbox/A",error:"leftover"}]};
      await loadJobs();
      assert.match(stable.textContent,/源目录清理待处理/);
      assert.match(stable.textContent,/leftover/);
    ''')


@pytest.mark.parametrize("failure", ["http", "network", "business", "nonjson"])
def test_stop_failures_are_visible_and_retryable(failure):
    run_js(r'''
      const card=jobNode({id:"j",stage:"downloading"}), stop=card.querySelector(".jstop");
      let refreshes=0;
      loadJobs=async()=>{refreshes++};
    ''' + {
        "http": 'fetch=async()=>response({error:"service down"},500);',
        "network": 'fetch=async()=>{throw new Error("offline")};',
        "business": 'fetch=async()=>response({ok:false,error:"busy"});',
        "nonjson": 'fetch=async()=>({ok:false,status:502,statusText:"Bad Gateway",text:async()=>"proxy down"});',
    }[failure] + r'''
      await stop.dispatch("click");
      assert.equal(stop.disabled,false);
      assert.equal(refreshes,1);
      assert.equal(flashes.length,1);
      assert.equal(flashes[0].error,true);
      assert.match(flashes[0].message,/停止失败/);
      fetch=async()=>response({ok:true});
      await stop.dispatch("click");
      assert.equal(stop.disabled,false);
      assert.match(flashes[1].message,/已请求停止/);
      assert.doesNotMatch(flashes[1].message,/已停止/);
    ''')


def test_slow_poll_is_single_flight_and_ticks_do_not_starve_accepted_data():
    run_js(r'''
      const pending=[];
      fetch=url=>{const d=deferred();pending.push({url,...d});return d.promise};
      const first=loadJobs(), tick=loadJobs(), anotherTick=loadJobs();
      assert.equal(pending.length,1); // No overlapping /api/jobs requests.
      pending[0].resolve(response({jobs:[{id:"slow",stage:"downloading"}]}));
      while(pending.length<2)await Promise.resolve();
      assert.deepEqual(lastJobs.map(j=>j.id),["slow"]); // Ticks do not discard a slow result.
      pending[1].resolve(response({tasks:[]}));
      while(pending.length<3)await Promise.resolve();
      pending[2].resolve(response({jobs:[{id:"new",stage:"done"}]}));
      while(pending.length<4)await Promise.resolve();
      pending[3].resolve(response({tasks:[]}));
      await Promise.all([first,tick,anotherTick]);
      assert.equal(pending.length,4); // Multiple ticks coalesce to exactly one follow-up cycle.
      assert.deepEqual(lastJobs.map(j=>j.id),["new"]);
      assert.equal(QUEUE_NODES.has("job:slow"),false);
    ''')


def test_mutation_refresh_invalidates_stale_jobs_and_panel_snapshots():
    run_js(r'''
      const pending=[];
      fetch=url=>{const d=deferred();pending.push({url,...d});return d.promise};
      const old=loadJobs();loadJobs(true); // A stop/delete/clear operation completed.
      pending[0].resolve(response({jobs:[{id:"deleted",stage:"downloading"}]}));
      while(pending.length<2)await Promise.resolve();
      assert.equal(QUEUE_NODES.has("job:deleted"),false);
      pending[1].resolve(response({jobs:[{id:"new",stage:"done"}]}));
      while(pending.length<3)await Promise.resolve();
      pending[2].resolve(response({tasks:[]}));await old;
      assert.deepEqual(lastJobs.map(j=>j.id),["new"]);
      pending.length=0;
      const frame=loadJobs();pending[0].resolve(response({jobs:[]}));
      while(pending.length<2)await Promise.resolve();
      loadJobs(true); // The panel subrequest must share the invalidation boundary.
      pending[1].resolve(response({tasks:[{id:"stale",phase:"PHASE_TYPE_RUNNING"}]}));
      while(pending.length<3)await Promise.resolve();
      assert.equal(QUEUE_NODES.has("xl:stale"),false);
      pending[2].resolve(response({jobs:[]}));
      while(pending.length<4)await Promise.resolve();
      pending[3].resolve(response({tasks:[{id:"latest",phase:"PHASE_TYPE_RUNNING"}]}));await frame;
      assert.equal(QUEUE_NODES.has("xl:latest"),true);
      fetch=async()=>response({},500);await loadJobs();
      assert.match(get("queue-hint").textContent,/刷新失败.*上次结果/);
      assert.equal(QUEUE_NODES.has("xl:latest"),true);
    ''')


def test_poll_timeout_releases_single_flight_and_can_refresh_again():
    run_js(r'''
      let deadline;
      setTimeout=fn=>{deadline=fn;return 1};clearTimeout=()=>{};
      fetch=(url,opts)=>new Promise((resolve,reject)=>{
        opts.signal.addEventListener("abort",()=>reject(new Error("fixture timeout")));
      });
      const timedOut=loadJobs();deadline();await timedOut;
      assert.equal(jobsInflight,null);
      assert.match(get("queue-hint").textContent,/timeout/);
      fetch=async url=>response(url.endsWith("/api/jobs")?{jobs:[]}:{tasks:[]});
      await loadJobs();assert.equal(jobsInflight,null);
      assert.equal(get("queue-hint").textContent,"");
    ''')


def test_settings_save_locks_inputs_reload_and_duplicate_submit_then_unlocks():
    run_js(r'''
      const row=settingsRow("A",{value:"old",type:"str",group:"fixture"});
      get("settings-list").append(row);
      const input=row.querySelector("input");input.value="draft";await input.dispatch("input");
      const blocked=new Node("button");blocked.disabled=true;get("settings-list").append(blocked);
      const d=deferred(), requests=[];
      fetch=async(url,opts)=>{requests.push({url,opts});return d.promise};
      const saving=saveSettings();
      assert.equal(input.disabled,true);
      assert.equal(get("settings-reload").disabled,true);
      renderDirty();assert.equal(get("settings-save").disabled,true);
      await saveSettings();await loadSettings();assert.equal(requests.length,1);
      assert.deepEqual(JSON.parse(requests[0].opts.body).updates,{A:"draft"});
      // Failed saves keep drafts and restore original disabled states.
      d.resolve(response({ok:false,error:"fixture failure"}));await saving;
      assert.equal(input.disabled,false);assert.equal(blocked.disabled,true);
      assert.equal(get("settings-reload").disabled,false);
      assert.equal(get("settings-save").disabled,false);
      assert.equal(settingsDirty.A,"draft");
      assert.match(get("settings-hint").textContent,/保存失败/);
      let count=0;
      fetch=async(url,opts)=>{count++;return response(opts.method==="POST"?{ok:true}:{ok:true,items:{
        A:{value:"draft",type:"str",group:"fixture"}}})};
      await saveSettings();
      assert.equal(count,2);assert.equal(settingsSaving,false);
      assert.equal(get("settings-list").querySelector("input").value,"draft");
      assert.equal(get("settings-list").querySelector("input").disabled,false);
      assert.equal(get("settings-save").disabled,true);
      assert.deepEqual(Object.keys(settingsDirty),[]);
    ''')


def test_settings_old_reload_cannot_rebuild_inputs_during_save():
    run_js(r'''
      const old=deferred(), post=deferred();let requests=0;
      fetch=async(url,opts)=>{requests++;return opts.method==="POST"?post.promise:old.promise};
      const reading=loadSettings();
      const row=settingsRow("A",{value:"old",type:"str"});get("settings-list").append(row);
      const input=row.querySelector("input");input.value="draft";await input.dispatch("input");
      const area=new Node("textarea");get("settings-list").append(area);
      const saving=saveSettings();
      old.resolve(response({ok:true,items:{A:{value:"stale",type:"str",group:"fixture"}}}));
      await reading;
      assert.equal(get("settings-list").querySelector("input"),input);
      assert.equal(input.disabled,true);assert.equal(area.disabled,true);
      assert.equal(settingsDirty.A,"draft");assert.equal(requests,2);
      post.resolve(response({ok:false,error:"retry fixture"}));await saving;
      assert.equal(input.disabled,false);assert.equal(area.disabled,false);
      assert.equal(settingsDirty.A,"draft");
      // Even after the save settles an old GET must remain obsolete.
      const late=deferred();fetch=async()=>late.promise;const stale=loadSettings();
      settingsDirty.A="draft";fetch=async()=>response({ok:false,error:"fixture"});await saveSettings();
      late.resolve(response({ok:true,items:{}}));await stale;
      assert.equal(settingsDirty.A,"draft");
    ''')


def test_settings_snapshot_preserves_new_drafts_and_partial_failures():
    run_js(r'''
      const row=settingsRow("A",{value:"original",type:"str"});get("settings-list").append(row);
      const input=row.querySelector("input");input.value="submitted";await input.dispatch("input");
      settingsDirty.B="failed draft";
      const d=deferred();let count=0;fetch=async()=>{count++;return d.promise};
      const saving=saveSettings();
      settingsDirty.A="newer draft";settingsDirty.C="late draft";
      d.resolve(response({ok:true,failed:{B:"invalid fixture"}}));await saving;
      assert.equal(count,1); // Do not reload and discard the unsent/failed drafts.
      assert.deepEqual({...settingsDirty},{A:"newer draft",B:"failed draft",C:"late draft"});
      assert.equal(settingsSaved.A,"submitted");assert.equal(settingsSaving,false);
      assert.match(get("settings-hint").textContent,/已保存 1 项.*失败 1 项/);
      input.value="submitted";await input.dispatch("input");
      assert.equal("A" in settingsDirty,false); // The successful baseline was advanced.
      assert.equal(get("settings-save").disabled,false);
    ''')


def test_archive_refreshes_changed_metadata_and_detail_popup_carries_actions():
    run_js(r'''
      const base={id:"a",code:"ABC",kind:"视频 1",cat:"video",path:"old",size_text:"1GB",
        count:1,files:["ABC.mp4"],at:"2026-10-25 00:00:00",meta:{
        title:"title",cover:"",maker:"old",rating:1,actors:["old"]}};
      syncFeed([base]);const first=mounted.get("a").node;
      syncFeed([{...base,meta:{...base.meta}}]);assert.equal(mounted.get("a").node,first);
      const next={...base,path:"new-path",meta:{...base.meta,maker:"new-maker",rating:9,actors:["new-actor"]},
        cleanup_pending:[{target:"inbox/A",error:"fixture leftover"}]};
      syncFeed([next]);const updated=mounted.get("a").node;
      assert.notEqual(updated,first);
      // 卡片上只留海报与标题：详情信息不在卡片上
      assert.equal(updated.querySelector(".card-body"),null,"卡片不该再有信息标签行");
      assert.equal(updated.querySelector(".card-foot"),null,"卡片不该再有底栏");
      assert.match(updated.querySelector(".card-title").textContent,/title/);
      assert.match(updated.textContent,/fixture leftover/);   // 清理待处理仍要提示
      for(const field of ["cat","path","cleanup_pending"])
        assert.notEqual(sigOf(base),sigOf({...base,[field]:"changed"}));
    ''')


def test_archive_card_shows_poster_only_and_opens_detail():
    """卡片只留海报 + 标题，点海报开详情弹层；详情里才有全部信息与操作。"""
    run_js(r'''
      const t={id:"a",code:"ABC-1",kind:"视频 1",cat:"video",size_text:"4.97GB",
        count:1,files:["ABC-1.mp4"],at:"2026-10-25 00:00:00",series:"ABC",link:"",
        path:"clouddrive2:dest/ABC-1",
        meta:{title:"长标题测试",cover:"https://img.invalid/c.jpg",maker:"片商",
              actors:["演员甲","演员乙"],rating:"8.5",duration:"120分钟",
              date:"2026-01-02",previews:["https://img.invalid/p1.jpg"]}};
      const node=card(t);
      // 卡片只有海报与标题
      assert.equal(node.querySelectorAll(".card-body").length,0);
      assert.equal(node.querySelectorAll(".card-foot").length,0);
      assert.equal(node.querySelectorAll(".actor-chip").length,0);
      assert.match(node.querySelector(".card-title").textContent,/长标题测试/);
      assert.match(node.querySelector(".card-code").textContent,/ABC-1/);
      // 详情信息不在卡片上
      for(const v of ["片商","演员甲","8.5","4.97GB"])
        assert.equal(node.textContent.includes(v),false,"卡片不该显示 "+v);

      // 点海报 → 详情弹层
      const cover=node.querySelector(".card-cover");
      await cover.dispatch("click",{});
      const layer=document.getElementById("detail-layer");
      assert.ok(layer,"点海报应打开详情弹层 | body children="+JSON.stringify(document.body.children.map(c=>c.id))+" | document.body.append="+typeof document.body.append);
      const text=layer.textContent;
      for(const v of ["片商","演员甲","8.5","4.97GB","ABC-1.mp4","120分钟","2026-01-02"])
        assert.ok(text.includes(v),"详情里应有 "+v);
      // 详情里有关键动作：改名 / 删除
      assert.ok(layer.querySelector(".dactions"),"详情里要有操作区");
      assert.match(layer.textContent,/改名/);
      assert.match(layer.textContent,/删除/);
    ''')


def test_archive_detail_popup_closes_by_escape_and_mask():
    run_js(r'''
      const t={id:"a",code:"ABC-1",kind:"视频 1",cat:"video",size_text:"1GB",count:1,
        files:["ABC-1.mp4"],at:"2026-10-25",path:"p",meta:{title:"x"}};
      const node=card(t);
      await node.querySelector(".card-cover").dispatch("click",{});
      let layer=document.getElementById("detail-layer");
      assert.ok(layer);
      layer.dispatch("click",{target:layer});          // 点遮罩
      assert.equal(document.getElementById("detail-layer"),null,"点遮罩应关闭");
      await node.querySelector(".card-cover").dispatch("click",{});
      layer=document.getElementById("detail-layer");
      assert.ok(layer);
      document.dispatchEvent({type:"keydown",key:"Escape"});  // Esc
      assert.equal(document.getElementById("detail-layer"),null,"Esc 应关闭");
    ''')


def test_meta_api_reports_nonjson_http_errors_and_keeps_method_contract():
    run_js(r'''
      let request;
      fetch=async(url,opts)=>{request={url,opts};return {ok:false,status:404,statusText:"Not Found",text:async()=>"missing route"}};
      await assert.rejects(metaApi("/api/scrape/status"),/404.*missing route.*scrape\/status/);
      assert.equal(request.opts.method,undefined);
      await assert.rejects(metaApi("/api/scrape/sync",{code:"fixture"}),/404/);
      assert.equal(request.opts.method,"POST");
      assert.deepEqual(JSON.parse(request.opts.body),{code:"fixture"});
    ''')


def test_seed_display_and_sort_do_not_turn_unknown_into_dead():
    run_js(r'''
      const values=[undefined,null,"",false,-1,1.5,"x","1.5",0,"0",5," 6 "];
      const items=values.map((seeders,i)=>({id:i,seeders,size_bytes:i,has_subtitle:true}));
      delete items[0].seeders;
      renderCandidates(items,"fixture",{});
      const rows=get("resource-slot").querySelector(".res-list").children;
      assert.equal(rows.filter(r=>r.classList.contains("dead")).length,2);
      assert.equal(rows.filter(r=>r.textContent.includes("做种数未知")).length,8);
      assert.equal(rows.filter(r=>r.textContent.includes("种子 0")).length,2);
      for(const mode of ["rec","seed","size"]){
        resState.sort=mode;const sorted=sortCandidates(items);
        assert.deepEqual(sorted.slice(-2).map(i=>seedCount(i.seeders)),[0,0]);
      }
    ''')
