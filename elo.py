<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Baseline — Daily Tennis Predictions</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=Hanken+Grotesque:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root{--bg:#100e0b;--panel:#191510;--panel-2:#211b14;--panel-3:#2a2117;--line:#33291d;
    --ink:#f3ead9;--muted:#9a8c76;--muted-2:#6f6354;--clay:#c8612f;--clay-bright:#e6743a;
    --grass:#5b8c4e;--win:#8bbf7a;--loss:#d2685f;--live:#e6743a;}
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(130% 80% at 82% -8%,#1c160f 0%,var(--bg) 55%);
    color:var(--ink);font-family:"Hanken Grotesque",sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
  .wrap{max-width:920px;margin:0 auto;padding:42px 20px 90px}
  header{display:flex;align-items:baseline;justify-content:space-between;gap:14px}
  h1{font-family:"Fraunces",serif;font-weight:900;font-size:clamp(34px,7vw,54px);letter-spacing:-.02em;margin:0;line-height:.95}
  h1 .dot{color:var(--clay)}
  .src{font-family:"Space Mono",monospace;font-size:11px;letter-spacing:.04em;color:var(--muted);text-align:right;line-height:1.5}
  .src b{color:var(--win)}
  .tagline{color:var(--muted);font-size:13.5px;margin:12px 0 26px;line-height:1.5;max-width:62ch}
  .datebar{display:flex;align-items:center;gap:6px;margin-bottom:22px;flex-wrap:wrap}
  .arrow{background:var(--panel-2);border:1px solid var(--line);color:var(--ink);width:34px;height:34px;border-radius:9px;cursor:pointer;font-size:15px;flex:none}
  .arrow:hover{border-color:#4a3b29}
  .pill{background:transparent;border:1px solid var(--line);color:var(--muted);padding:7px 12px;border-radius:9px;cursor:pointer;font-size:12.5px;font-weight:600;white-space:nowrap;transition:.15s}
  .pill:hover{color:var(--ink);border-color:#4a3b29}
  .pill.active{background:var(--clay);border-color:var(--clay);color:#1a120b}
  .pill .d2{display:block;font-family:"Space Mono",monospace;font-size:10px;font-weight:400;opacity:.7;margin-top:1px}
  .bubbles{display:flex;gap:14px;margin-bottom:8px}
  .bubble{flex:1;background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:16px;padding:18px 12px;text-align:center}
  .bubble .num{font-family:"Fraunces",serif;font-weight:900;font-size:clamp(28px,6vw,40px);line-height:1;letter-spacing:-.01em}
  .bubble .lab{font-family:"Space Mono",monospace;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-top:8px}
  .bubble.acc .num{color:var(--clay-bright)} .bubble.won .num{color:var(--win)}
  .daynote{font-family:"Space Mono",monospace;font-size:11px;color:var(--muted-2);margin:12px 0 22px;letter-spacing:.03em}
  .filters{display:flex;gap:7px;margin-bottom:18px;flex-wrap:wrap}
  .chip{background:transparent;border:1px solid var(--line);color:var(--muted);padding:6px 13px;border-radius:20px;cursor:pointer;font-size:12px;font-weight:600;transition:.15s}
  .chip:hover{color:var(--ink)} .chip.active{background:var(--panel-3);border-color:#56442f;color:var(--ink)}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:12px;position:relative;overflow:hidden}
  .card.correct{border-left:3px solid var(--win)} .card.wrong{border-left:3px solid var(--loss)} .card.live{border-left:3px solid var(--live)}
  .card-top{display:flex;align-items:center;gap:9px;margin-bottom:13px}
  .tier{font-family:"Space Mono",monospace;font-size:10px;font-weight:700;letter-spacing:.1em;padding:3px 7px;border-radius:5px;color:#1a120b;background:var(--clay)}
  .tier.WTA{background:#c06a93} .tier.CHALLENGER{background:var(--grass)}
  .tourn{color:var(--muted);font-size:12.5px}
  .lowconf{font-size:10px;font-family:"Space Mono",monospace;color:var(--muted-2);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
  .status{margin-left:auto;display:flex;align-items:center;gap:7px;font-family:"Space Mono",monospace;font-size:11px;letter-spacing:.06em}
  .live-tag{color:var(--live);text-transform:uppercase;display:flex;align-items:center;gap:6px}
  .pulse{width:8px;height:8px;border-radius:50%;background:var(--live);animation:pulse 1.2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(230,116,58,.6)}70%{box-shadow:0 0 0 8px rgba(230,116,58,0)}100%{box-shadow:0 0 0 0 rgba(230,116,58,0)}}
  .sched{color:var(--muted)}
  .mark{font-size:16px;font-weight:700;width:24px;height:24px;border-radius:50%;display:grid;place-items:center}
  .mark.ok{color:var(--win);background:rgba(139,191,122,.12)} .mark.no{color:var(--loss);background:rgba(210,104,95,.12)}
  .row{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:12px;padding:5px 0}
  .pname{font-size:16.5px;font-weight:500}
  .pname .pick{font-size:10px;font-family:"Space Mono",monospace;color:var(--clay-bright);border:1px solid #56442f;border-radius:4px;padding:1px 5px;margin-left:8px;letter-spacing:.05em;vertical-align:middle}
  .serving::after{content:"●";color:var(--clay-bright);font-size:9px;margin-left:7px;vertical-align:middle}
  .winner{color:var(--win);font-weight:700}
  .sets{font-family:"Space Mono",monospace;font-size:15px;letter-spacing:.16em;font-variant-numeric:tabular-nums}
  .game{font-family:"Space Mono",monospace;font-size:15px;font-weight:700;min-width:30px;text-align:right;color:var(--clay-bright);font-variant-numeric:tabular-nums}
  .pred{display:flex;margin-top:13px;height:26px;border-radius:7px;overflow:hidden;border:1px solid var(--line)}
  .pred .bar{height:100%;display:flex;align-items:center;padding:0 9px;font-family:"Space Mono",monospace;font-size:11.5px;font-weight:700;white-space:nowrap}
  .pred .a{background:rgba(200,97,47,.26);justify-content:flex-start} .pred .b{background:rgba(255,255,255,.045);color:var(--muted);justify-content:flex-end}
  .empty{color:var(--muted);text-align:center;padding:50px 0;font-style:italic}
  footer{margin-top:36px;color:var(--muted-2);font-size:11px;line-height:1.6;border-top:1px solid var(--line);padding-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Baseline<span class="dot">.</span></h1>
    <div class="src" id="src">loading…</div>
  </header>
  <p class="tagline">Every match we can predict, listed by day, with a model win probability,
    live point-by-point scores, and a ✓ / ✗ once each match settles.</p>

  <div class="datebar" id="datebar"></div>
  <div class="bubbles" id="bubbles"></div>
  <div class="daynote" id="daynote"></div>
  <div class="filters" id="filters"></div>
  <div id="list"><div class="empty">Loading matches…</div></div>

  <footer>
    The model picks the higher-probability player; ✓ = pick won, ✗ = pick lost. “Accuracy”
    counts only settled matches for the selected day. “low-confidence” marks matches where a
    player couldn’t be matched to the rating model yet. Not betting advice.
  </footer>
</div>

<script>
const DOW=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"], MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function ymd(d){return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");}
function addDays(d,n){const x=new Date(d);x.setDate(x.getDate()+n);return x;}
function startOfDay(d){const x=new Date(d);x.setHours(0,0,0,0);return x;}

let selected = startOfDay(new Date());
let tierFilter = "ALL";
let current = [];                 // matches for selected day
const byId = {};                  // id -> match (for live WS updates)

async function fetchDay(){
  const r = await fetch("/api/matches?date="+ymd(selected));
  if(!r.ok){ current=[]; return; }
  current = await r.json();
  for(const k in byId) delete byId[k];
  current.forEach(m=>byId[m.id]=m);
}

function statsFor(matches){
  let picks=0, correct=0;
  matches.forEach(m=>{ if(m.correct!==null && m.correct!==undefined){ picks++; if(m.correct) correct++; }});
  return {picks, correct, acc: picks?Math.round(correct/picks*100):0};
}

function renderDatebar(){
  const bar=document.getElementById("datebar"); bar.innerHTML="";
  const prev=document.createElement("button"); prev.className="arrow"; prev.textContent="‹";
  prev.onclick=()=>{selected=addDays(selected,-1);reload();}; bar.appendChild(prev);
  for(let off=-3;off<=3;off++){
    const d=addDays(startOfDay(new Date()),off);
    const b=document.createElement("button"); b.className="pill"+(ymd(d)===ymd(selected)?" active":"");
    const lab=off===0?"Today":off===-1?"Yest.":off===1?"Tom.":DOW[d.getDay()];
    b.innerHTML=`${lab}<span class="d2">${MON[d.getMonth()]} ${d.getDate()}</span>`;
    b.onclick=()=>{selected=d;reload();}; bar.appendChild(b);
  }
  const next=document.createElement("button"); next.className="arrow"; next.textContent="›";
  next.onclick=()=>{selected=addDays(selected,1);reload();}; bar.appendChild(next);
}

function renderFilters(){
  const f=document.getElementById("filters"); f.innerHTML="";
  [["ALL","All"],["ATP","ATP"],["WTA","WTA"],["CHALLENGER","Challenger"]].forEach(([k,lab])=>{
    const c=document.createElement("button"); c.className="chip"+(tierFilter===k?" active":"");
    c.textContent=lab; c.onclick=()=>{tierFilter=k;renderList();renderBubbles();}; f.appendChild(c);
  });
}

function filtered(){ return current.filter(m=>tierFilter==="ALL"||m.tier===tierFilter); }

function renderBubbles(){
  const s=statsFor(filtered());
  document.getElementById("bubbles").innerHTML=`
    <div class="bubble"><div class="num">${s.picks}</div><div class="lab">Picks settled</div></div>
    <div class="bubble won"><div class="num">${s.correct}</div><div class="lab">Correct</div></div>
    <div class="bubble acc"><div class="num">${s.picks?s.acc+"%":"—"}</div><div class="lab">Accuracy</div></div>`;
  const today=startOfDay(new Date()), note=document.getElementById("daynote");
  if(selected>today) note.textContent="Upcoming day — predictions lock in; results grade once matches finish.";
  else if(selected.getTime()===today.getTime()) note.textContent="Today — accuracy updates live as matches settle.";
  else note.textContent="Completed day — final results.";
}

function timeStr(iso){const d=new Date(iso);return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");}
function lastName(n){return n.split(" ").pop();}

function cardHtml(m){
  const sc=m.score||{}, st=m.status, fin=st==="finished", live=st==="live";
  let statusHtml;
  if(fin && m.correct!==null && m.correct!==undefined) statusHtml=`<span class="mark ${m.correct?'ok':'no'}">${m.correct?'✓':'✗'}</span>`;
  else if(live) statusHtml=`<span class="live-tag"><span class="pulse"></span>Live</span>`;
  else if(fin) statusHtml=`<span class="sched">Final</span>`;
  else statusHtml=`<span class="sched">${timeStr(m.scheduled)}</span>`;
  const p=m.prediction||{}, probA=p.prob_a==null?0.5:p.prob_a, probB=1-probA;
  const favName=lastName(probA>=0.5?m.player_a:m.player_b), dogName=lastName(probA>=0.5?m.player_b:m.player_a);
  const favP=Math.round(Math.max(probA,probB)*100), dogP=100-favP;
  const pickA=m.predicted_winner==="a", pickB=m.predicted_winner==="b";
  const servA=live&&sc.server==="a", servB=live&&sc.server==="b";
  const wonA=fin&&sc.winner==="a", wonB=fin&&sc.winner==="b";
  const setsA=(sc.sets_a||[]).join(" "), setsB=(sc.sets_b||[]).join(" ");
  const gameA=live?sc.game_a:"", gameB=live?sc.game_b:"";
  const surf=(m.surface&&m.surface!=="Unknown")?" · "+m.surface:"";
  const lc=(p.confident===false)?`<span class="lowconf">low-confidence</span>`:"";
  const nm=(name,pick,serv,won)=>`<span class="pname${serv?' serving':''}${won?' winner':''}">${name}${pick?'<span class="pick">PICK</span>':''}</span>`;
  return `
    <div class="card-top">
      <span class="tier ${m.tier}">${m.tier}</span>
      <span class="tourn">${m.tournament}${surf}</span>${lc}
      <span class="status">${statusHtml}</span>
    </div>
    <div class="row">${nm(m.player_a,pickA,servA,wonA)}<span class="sets">${setsA}</span><span class="game">${gameA}</span></div>
    <div class="row">${nm(m.player_b,pickB,servB,wonB)}<span class="sets">${setsB}</span><span class="game">${gameB}</span></div>
    <div class="pred">
      <div class="bar a" style="width:${(probA>=0.5?probA:probB)*100}%">${favName} ${favP}%</div>
      <div class="bar b" style="width:${(probA>=0.5?probB:probA)*100}%">${dogP}% ${dogName}</div>
    </div>`;
}

function renderList(){
  const list=document.getElementById("list"), shown=filtered();
  if(!shown.length){ list.innerHTML='<div class="empty">No matches for this day.</div>'; return; }
  list.innerHTML="";
  shown.forEach(m=>{
    const fin=m.status==="finished", live=m.status==="live";
    const el=document.createElement("div");
    el.className="card"+(fin?(m.correct?" correct":(m.correct===false?" wrong":"")):live?" live":"");
    el.id="card-"+m.id; el.innerHTML=cardHtml(m);
    list.appendChild(el);
  });
}

async function reload(){
  renderDatebar(); renderFilters();
  document.getElementById("list").innerHTML='<div class="empty">Loading matches…</div>';
  await fetchDay();
  renderBubbles(); renderList();
}

// live updates pushed from the server poller
function connectWS(){
  const proto=location.protocol==="https:"?"wss":"ws";
  const ws=new WebSocket(`${proto}://${location.host}/ws/live`);
  const src=document.getElementById("src");
  ws.onopen=()=>src.innerHTML='feed <b>connected</b>';
  ws.onclose=()=>{src.textContent="reconnecting…";setTimeout(connectWS,1500);};
  ws.onmessage=ev=>{
    const msg=JSON.parse(ev.data);
    if(msg.type!=="score") return;
    const m=byId[msg.match_id]; if(!m) return;     // not on the current day
    m.score=msg.score; m.status=msg.score.status;
    if(msg.score.status==="finished" && m.predicted_winner && (msg.score.winner==="a"||msg.score.winner==="b")){
      m.correct=(m.predicted_winner===msg.score.winner);
    }
    const el=document.getElementById("card-"+m.id);
    if(el && (tierFilter==="ALL"||m.tier===tierFilter)){
      const fin=m.status==="finished";
      el.className="card"+(fin?(m.correct?" correct":(m.correct===false?" wrong":"")):m.status==="live"?" live":"");
      el.innerHTML=cardHtml(m);
    }
    renderBubbles();
  };
}

reload().then(connectWS);
// gentle refresh so newly-started or finished matches appear even without a WS tick
setInterval(()=>{ if(ymd(selected)===ymd(startOfDay(new Date()))) reload(); }, 30000);
</script>
</body>
</html>
