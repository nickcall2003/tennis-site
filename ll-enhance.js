/* ll-enhance.js — extracted from index.html to keep it phone-committable.
   Personalization (favorites/My Board/accent), Calibration view, Team profiles.
   Loads AFTER the main inline script, so it shares its global functions. */
/* ===== Line Logic personalization: favorites, My Board, accent ===== */
(function(){
  function _read(){try{return JSON.parse(localStorage.getItem("ll_favs")||"[]");}catch(e){return[];}}
  function _write(a){try{localStorage.setItem("ll_favs",JSON.stringify(a));}catch(e){}}
  function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  window.isFav=function(k){return _read().some(function(x){return x.k===k;});};
  window.favKey=function(sport,name){return sport+"|"+name;};
  window.toggleFav=function(k,label,sport,btn){
    var a=_read(),i=-1;for(var j=0;j<a.length;j++){if(a[j].k===k){i=j;break;}}
    var on;if(i>=0){a.splice(i,1);on=false;}else{a.push({k:k,l:label,s:sport});on=true;}
    _write(a);
    if(btn){btn.classList.toggle("on",on);btn.textContent=on?"\u2605":"\u2606";btn.setAttribute("aria-pressed",on?"true":"false");btn.title=on?"In My Board":"Add to My Board";}
    return on;
  };
  window.favBtn=function(sport,name){
    if(!name)return "";
    var k=favKey(sport,name),on=isFav(k);
    return '<button class="ll-fav'+(on?" on":"")+'" data-fk="'+esc(k)+'" data-fl="'+esc(name)+'" data-fs="'+esc(sport)+'" '+
           'aria-pressed="'+(on?"true":"false")+'" title="'+(on?"In My Board":"Add to My Board")+'">'+(on?"\u2605":"\u2606")+'</button>';
  };
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest(".ll-fav");
    if(b){e.stopPropagation();toggleFav(b.dataset.fk,b.dataset.fl,b.dataset.fs,b);
      if(view==="myboard")setTimeout(renderMyBoard,0);}
  });
  window.openMyBoard=function(){
    view="myboard";if(typeof detailId!=="undefined"&&detailId&&typeof closeDetail==="function")closeDetail();
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var dt=document.getElementById("detail");if(dt)dt.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent="My Board";
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.toggle("active",it.dataset.view==="myboard");});
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    if(typeof toggleMenu==="function")toggleMenu(false);
    renderMyBoard();
  };
  function renderMyBoard(){
    var list=document.getElementById("list");if(!list)return;
    var favs=_read();
    if(!favs.length){
      list.innerHTML='<div class="mb-wrap"><div class="mb-intro">Your personal board. Tap the \u2606 star on any predicted winner \u2014 team or player \u2014 to pin it here.</div>'+
        '<div class="mb-empty"><div class="mb-empty-star">\u2606</div><div class="mb-empty-h">No favorites yet</div>'+
        '<p>Open any game\u2019s prediction and tap the star next to the predicted winner. Your teams and players collect here for one-tap access to their matchups.</p></div></div>';
      return;
    }
    var bySport={};favs.forEach(function(f){(bySport[f.s]=bySport[f.s]||[]).push(f);});
    var h='<div class="mb-wrap"><div class="mb-intro">Your pinned teams and players. Tap a row to jump to that sport\u2019s slate; tap the star to remove.</div>';
    Object.keys(bySport).forEach(function(s){
      var lbl=(typeof SPORT_LABEL!=="undefined"&&SPORT_LABEL[s])||s.toUpperCase();
      h+='<div class="mb-group"><div class="mb-group-h">'+esc(lbl)+'</div>';
      bySport[s].forEach(function(f){
        h+='<div class="mb-row" data-jump="'+esc(s)+'"><span class="mb-name">'+esc(f.l)+'</span>'+
           '<button class="ll-fav on mb-x" data-fk="'+esc(f.k)+'" data-fl="'+esc(f.l)+'" data-fs="'+esc(s)+'" title="Remove">\u2605</button></div>';
      });
      h+='</div>';
    });
    h+='</div>';list.innerHTML=h;
    list.querySelectorAll(".mb-row").forEach(function(r){
      r.addEventListener("click",function(e){
        if(e.target.closest(".ll-fav"))return;
        if(typeof setSport==="function")setSport(r.dataset.jump);
      });
    });
  }
  window.renderMyBoard=renderMyBoard;
  function applyAccent(c){
    document.documentElement.style.setProperty("--accent",c);
    document.querySelectorAll(".ac-opt").forEach(function(b){b.classList.toggle("active",b.dataset.ac===c);});
  }
  /* Theme presets: surface/background palettes (accent stays independent).
     Semantic --win/--loss are intentionally left alone so meaning never shifts. */
  var THEMES={
    slate:{"--bg":"#0e1014","--panel":"#171a20","--panel-2":"#1e222a","--panel-3":"#262b35","--line":"#2f3540","--ink":"#eef1f5","--muted":"#9aa3b0","--muted-2":"#6b7382","--glow":"#1c160f"},
    cyber:{"--bg":"#05060a","--panel":"#0b0f16","--panel-2":"#0f1520","--panel-3":"#16202e","--line":"#1b2838","--ink":"#e8fbff","--muted":"#7f97a8","--muted-2":"#4f6474","--glow":"#08222b"},
    obsidian:{"--bg":"#000000","--panel":"#0c0c0e","--panel-2":"#141417","--panel-3":"#1c1c21","--line":"#26262d","--ink":"#f4f4f6","--muted":"#9a9aa6","--muted-2":"#6a6a76","--glow":"#111111"},
    midnight:{"--bg":"#080b1a","--panel":"#0f1428","--panel-2":"#151b34","--panel-3":"#1e2645","--line":"#2a3358","--ink":"#eaefff","--muted":"#94a0c8","--muted-2":"#616d94","--glow":"#101a3a"}
  };
  function applyTheme(k){
    var t=THEMES[k]||THEMES.slate,root=document.documentElement;
    for(var v in t){root.style.setProperty(v,t[v]);}
    document.querySelectorAll(".th-opt").forEach(function(b){b.classList.toggle("active",b.dataset.th===k);});
  }
  var savedTh="slate";try{savedTh=localStorage.getItem("ll_theme")||"slate";}catch(e){}
  applyTheme(savedTh);
  document.querySelectorAll(".th-opt").forEach(function(b){
    b.addEventListener("click",function(){var k=b.dataset.th;try{localStorage.setItem("ll_theme",k);}catch(e){}applyTheme(k);});
  });
  var savedAc="#3ad17a";try{savedAc=localStorage.getItem("ll_accent")||"#3ad17a";}catch(e){}
  applyAccent(savedAc);
  document.querySelectorAll(".ac-opt").forEach(function(b){
    b.addEventListener("click",function(){var c=b.dataset.ac;try{localStorage.setItem("ll_accent",c);}catch(e){}applyAccent(c);});
  });
})();
/* ===== Calibration (reliability) view ===== */
(function(){
  function setupCalView(){
    view="calibration";
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var dt=document.getElementById("detail");if(dt)dt.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent="Calibration";
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.toggle("active",it.dataset.view==="calibration");});
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    if(typeof toggleMenu==="function")toggleMenu(false);
  }
  window.openCalibration=async function(){
    setupCalView();
    var list=document.getElementById("list");
    list.innerHTML='<div class="cal-wrap"><div class="cal-loading">Loading reliability data\u2026</div></div>';
    var d;try{d=await getJSON("/api/calibration");}catch(e){
      list.innerHTML='<div class="cal-wrap"><div class="cal-empty">Couldn\u2019t load calibration data right now.</div></div>';return;}
    renderCalibration(d);
  };
  function renderCalibration(d){
    var list=document.getElementById("list");
    var b=(d&&d.buckets)||[];
    var intro='<div class="cal-intro"><div class="cal-h">Are our probabilities honest?</div>'+
      '<p>When the model says a pick has an <b>X% chance</b>, how often does it actually win? A well-calibrated model tracks reality \u2014 an 80% pick should win about 80% of the time. Built only from picks with a stored probability, so it sharpens as more settle.</p></div>';
    if(!b.length){
      list.innerHTML='<div class="cal-wrap">'+intro+'<div class="cal-empty">Not enough settled picks with a stored probability yet. The curve fills in as today\u2019s and future picks grade out.</div></div>';return;}
    var rows='';
    b.forEach(function(x){
      var diff=x.actual-x.predicted,ad=Math.abs(diff),cls=ad<=6?"ok":(ad<=12?"mid":"off");
      rows+='<div class="cal-row"><div class="cal-lbl">'+x.lo+'\u2013'+x.hi+'%</div>'+
        '<div class="cal-track"><div class="cal-fill '+cls+'" style="width:'+Math.max(2,Math.min(100,x.actual))+'%"></div>'+
        '<div class="cal-mark" style="left:'+Math.max(0,Math.min(100,x.predicted))+'%"></div></div>'+
        '<div class="cal-val">'+x.actual+'%<span> actual \u00b7 n'+x.n+'</span></div></div>';
    });
    var brier=(d.brier!=null)?('<div class="cal-stat"><div class="cal-stat-v">'+d.brier+'</div><div class="cal-stat-k">Brier score<span>lower is sharper \u00b7 .25 = coin flip</span></div></div>'):'';
    var samp='<div class="cal-stat"><div class="cal-stat-v">'+(d.n||0)+'</div><div class="cal-stat-k">settled picks scored</div></div>';
    list.innerHTML='<div class="cal-wrap">'+intro+
      '<div class="cal-stats">'+brier+samp+'</div>'+
      '<div class="cal-legend"><span class="cal-key-fill"></span> actual win rate <span class="cal-key-mark"></span> what the model claimed</div>'+
      '<div class="cal-chart">'+rows+'</div>'+
      '<div class="cal-foot">Green rows = claim and reality agree within 6 points. Same honest results the model is graded on \u2014 nothing hand-picked.</div></div>';
  }
})();
/* ===== Recent Results (on-site receipts) ===== */
(function(){
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function setup(){
    view="results";
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var d=document.getElementById("detail");if(d)d.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent="Recent Results";
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.toggle("active",it.dataset.view==="results");});
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    if(typeof toggleMenu==="function")toggleMenu(false);
  }
  function fmtDay(iso){try{var p=iso.split("-");return new Date(p[0],p[1]-1,p[2]).toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"});}catch(e){return iso;}}
  window.openResults=async function(){
    setup();
    var list=document.getElementById("list");
    list.innerHTML='<div class="rs-wrap"><div class="rs-loading">Loading results\u2026</div></div>';
    var d;try{d=await getJSON("/api/results/recent?days=7");}catch(e){
      list.innerHTML='<div class="rs-wrap"><div class="rs-empty">Couldn\u2019t load results right now.</div></div>';return;}
    var days=(d&&d.days)||[];
    var intro='<div class="rs-intro">Every graded pick, win or lose \u2014 no deleted losers. The same record the model is scored on.</div>';
    if(!days.length){list.innerHTML='<div class="rs-wrap">'+intro+'<div class="rs-empty">No graded results yet. They fill in here as games settle.</div></div>';return;}
    var sum=(d.summary||{});
    var h='<div class="rs-wrap">'+intro+
      '<div class="rs-sum"><div class="rs-sum-v">'+esc(sum.record||"")+'</div><div class="rs-sum-k">last '+days.length+' days \u00b7 model record</div></div>';
    days.forEach(function(day){
      h+='<div class="rs-day"><div class="rs-day-h"><span>'+esc(fmtDay(day.date))+'</span><span class="rs-day-rec">'+esc(day.record)+'</span></div>';
      day.picks.forEach(function(p){
        h+='<div class="rs-row"><span class="rs-res '+(p.won?"w":"l")+'">'+(p.won?"W":"L")+'</span>'+
          '<span class="rs-pick">'+esc(p.pick)+'</span>'+
          '<span class="rs-meta">'+(p.prob)+'% \u00b7 '+esc((p.sport||"").toUpperCase())+'</span></div>';
      });
      h+='</div>';
    });
    h+='<div class="rs-foot">Same tracked model that grades itself on the Calibration page.</div></div>';
    list.innerHTML=h;
  };
})();
/* ===== Promote: shareable posts from today's picks ===== */
(function(){
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function setup(){
    view="promote";
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var d=document.getElementById("detail");if(d)d.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent="Promote";
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.toggle("active",it.dataset.view==="promote");});
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    if(typeof toggleMenu==="function")toggleMenu(false);
  }
  function copyText(t,btn){
    try{navigator.clipboard.writeText(t);}catch(e){
      var ta=document.createElement("textarea");ta.value=t;document.body.appendChild(ta);ta.select();
      try{document.execCommand("copy");}catch(_){}document.body.removeChild(ta);}
    if(btn){var o=btn.textContent;btn.textContent="Copied \u2713";setTimeout(function(){btn.textContent=o;},1400);}
  }
  function _card(kind, title, meta, text, discord){
    var id="pm-"+kind;
    var btns='<button class="pm-btn pm-copy" data-t="'+id+'">Copy'+(discord?"":" for X")+'</button>';
    if(discord){btns+=' <button class="pm-btn pm-alt pm-send" data-kind="'+kind.replace("d-","")+'">Send to Discord</button><span class="pm-note"></span>';}
    return '<div class="pm-card"><div class="pm-h">'+title+(meta?'<span>'+meta+'</span>':"")+'</div>'+
      '<pre class="pm-text" id="'+id+'">'+esc(text)+'</pre>'+btns+'</div>';
  }
  function _ah(){try{return (typeof authHeaders==="function")?authHeaders():{};}catch(e){return{};}}
  async function getAuthed(u){var r=await fetch(u,{cache:"no-store",headers:_ah()});if(!r.ok)throw 0;return r.json();}
  window.openPromote=async function(){
    setup();
    var list=document.getElementById("list");
    list.innerHTML='<div class="pm-wrap"><div class="pm-loading">Building today\u2019s posts\u2026</div></div>';
    var pv={},rc={};
    try{pv=await getAuthed("/api/promo/preview");}catch(e){}
    try{rc=await getAuthed("/api/promo/recap");}catch(e){}
    if(pv&&pv.error==="forbidden"){list.innerHTML='<div class="pm-wrap"><div class="pm-empty">This is the owner-only promotion panel.</div></div>';return;}
    if(!pv||!pv.x){list.innerHTML='<div class="pm-wrap"><div class="pm-empty">Couldn\u2019t load posts. Make sure you\u2019re logged in as the owner account and that ADMIN_USERNAME is set, then hard-refresh.</div></div>';return;}
    var h='<div class="pm-wrap"><div class="pm-intro">Ready-to-post content from your real data \u2014 it leads with your public track record, the account\u2019s real edge. Copy to X, or push straight to Discord.</div>';
    h+='<div class="pm-sec">Today\u2019s picks</div>';
    h+=_card("x-picks","X / Twitter",(pv.x||"").length+"/280",pv.x||"",false);
    h+=_card("d-picks","Discord","",pv.discord||"",true);
    if(rc&&rc.has_results){
      h+='<div class="pm-sec">Yesterday\u2019s recap &middot; '+esc(rc.record||"")+'</div>';
      h+=_card("x-recap","X / Twitter",(rc.x||"").length+"/280",rc.x||"",false);
      h+=_card("d-recap","Discord","",rc.discord||"",true);
    }
    h+='<div class="pm-tip">The recap is your highest-value post \u2014 people follow accounts that show results, win or lose. Post picks in the morning, the recap the next day, at a consistent time.</div></div>';
    list.innerHTML=h;
    list.querySelectorAll(".pm-copy").forEach(function(b){b.addEventListener("click",function(){
      var el=document.getElementById(b.dataset.t);copyText(el?el.textContent:"",b);});});
    list.querySelectorAll(".pm-send").forEach(function(b){b.addEventListener("click",async function(){
      var note=b.parentElement.querySelector(".pm-note");b.textContent="Sending\u2026";
      try{var r=await fetch("/api/promo/discord?kind="+encodeURIComponent(b.dataset.kind),{method:"POST",headers:_ah()});
        var j=await r.json();if(note)note.textContent=j.ok?" Posted \u2713":(" "+(j.error||"failed"));}
      catch(e){if(note)note.textContent=" failed";}
      b.textContent="Send to Discord";});});
  };
  window.checkPromoteAccess=function(){
    try{
      fetch("/api/promo/allowed",{headers:_ah()}).then(function(r){return r.json();}).then(function(j){
        var nav=document.getElementById("promote-nav");if(nav)nav.style.display=(j&&j.admin)?"":"none";
      }).catch(function(){});
    }catch(e){}
  };
  checkPromoteAccess();
})();
/* ===== AI Assistant — floating chat bubble ===== */
(function(){
  var hist=[], built=false, open=false;
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function _fmt(t){return t.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/`([^`]+)`/g,"<code>$1</code>");}
  function mdToHtml(s){
    var e=function(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");};
    var out="",inList=false;
    String(s==null?"":s).split("\n").forEach(function(ln){
      var m=ln.match(/^\s*[-\u2022]\s+(.*)$/);
      if(m){if(!inList){out+="<ul>";inList=true;}out+="<li>"+_fmt(e(m[1]))+"</li>";}
      else{if(inList){out+="</ul>";inList=false;}out+=ln.trim()===""?"":("<div>"+_fmt(e(ln))+"</div>");}
    });
    if(inList)out+="</ul>";
    return out;
  }
  function render(){
    var log=document.getElementById("chat-log");if(!log)return;
    log.innerHTML=hist.map(function(m){
      var inner=m.role==="assistant"?mdToHtml(m.content):esc(m.content).replace(/\n/g,"<br>");
      return '<div class="ch-msg ch-'+m.role+'">'+inner+'</div>';
    }).join("")+(window._chatBusy?'<div class="ch-msg ch-assistant ch-typing"><span></span><span></span><span></span></div>':"");
    log.scrollTop=log.scrollHeight;
  }
  function _favs(){try{return JSON.parse(localStorage.getItem("ll_favs")||"[]");}catch(e){return[];}}
  function hideChips(){var c=document.getElementById("chat-chips");if(c)c.style.display="none";}
  async function ask(msg){
    msg=(msg||"").trim();if(!msg||window._chatBusy)return;
    hist.push({role:"user",content:msg});window._chatBusy=true;render();hideChips();
    try{
      var r=await fetch("/api/chat",{method:"POST",headers:{"content-type":"application/json"},
        body:JSON.stringify({message:msg,history:hist.slice(0,-1),favorites:_favs()})});
      var d=await r.json();
      hist.push({role:"assistant",content:d.reply||"Sorry, something went wrong."});
    }catch(e){hist.push({role:"assistant",content:"I couldn\u2019t reach the assistant just now."});}
    window._chatBusy=false;render();
  }
  function send(){var inp=document.getElementById("chat-input");if(!inp)return;var m=inp.value;inp.value="";ask(m);}
  function seed(){if(!hist.length){hist.push({role:"assistant",content:"Hey! Ask me who the model likes in any of today\u2019s games."});}}
  function build(){
    if(built||!document.body)return;built=true;
    var fab=document.createElement("button");
    fab.id="ll-chat-fab";fab.type="button";fab.setAttribute("aria-label","Ask the assistant");fab.innerHTML="\uD83D\uDCAC";
    var pop=document.createElement("div");pop.id="ll-chat-pop";pop.className="ch-pop";pop.style.display="none";
    pop.innerHTML='<div class="ch-pop-head"><span>\uD83C\uDFAF Assistant</span>'+
      '<button class="ch-clear" id="chat-clear">Clear</button><button class="ch-close" id="ch-close" aria-label="Close">\u00D7</button></div>'+
      '<div class="ch-log" id="chat-log"></div>'+
      '<div class="ch-chips" id="chat-chips">'+
        '<button class="ch-chip">Who does the model like today?</button>'+
        '<button class="ch-chip">How accurate is the model?</button>'+
        '<button class="ch-chip">Strongest pick tonight?</button></div>'+
      '<div class="ch-bar"><input id="chat-input" class="ch-input" placeholder="Ask about today\u2019s games\u2026" autocomplete="off"><button id="chat-send" class="ch-send">Send</button></div>';
    document.body.appendChild(fab);document.body.appendChild(pop);
    fab.addEventListener("click",function(){setOpen(!open);});
    document.getElementById("ch-close").addEventListener("click",function(){setOpen(false);});
    document.getElementById("chat-send").addEventListener("click",send);
    document.getElementById("chat-input").addEventListener("keydown",function(e){if(e.key==="Enter"){e.preventDefault();send();}});
    pop.querySelectorAll(".ch-chip").forEach(function(b){b.addEventListener("click",function(){ask(b.textContent);});});
    document.getElementById("chat-clear").addEventListener("click",function(){hist=[];window._chatBusy=false;seed();render();var c=document.getElementById("chat-chips");if(c)c.style.display="";});
  }
  function setOpen(o){
    open=o;var pop=document.getElementById("ll-chat-pop"),fab=document.getElementById("ll-chat-fab");
    if(pop)pop.style.display=o?"flex":"none";
    if(fab)fab.classList.toggle("open",o);
    if(o){seed();render();if(hist.length>1)hideChips();var inp=document.getElementById("chat-input");if(inp)setTimeout(function(){inp.focus();},50);}
  }
  window.openChat=function(){build();setOpen(true);};
  if(document.body)build();else document.addEventListener("DOMContentLoaded",build);
})();
/* ===== Team profile pages ===== */
(function(){
  var SUP={nba:1,nfl:1,ncaaf:1,ncaab:1,wncaab:1,mlb:1,nhl:1,soccer:1};
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;");}
  window.teamLinks=function(g,sp){
    if(!SUP[sp]||!g||!g.home||!g.away)return "";
    if(!g.home.team_id||!g.away.team_id)return "";
    var lg=(sp==="soccer"?(g.league||""):"");
    function btn(side){var t=g[side],nm=t.name||t.abbr||(side==="home"?"Home":"Away");
      return '<button class="tp-link" data-tp-sport="'+esc(sp)+'" data-tp-id="'+esc(t.team_id)+'" data-tp-name="'+esc(nm)+'" data-tp-league="'+esc(lg)+'">'+esc(t.abbr||nm)+' profile \u203a</button>';}
    return '<div class="tp-links">'+btn("home")+btn("away")+'</div>';
  };
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest(".tp-link");
    if(b){e.preventDefault();e.stopPropagation();openTeamProfile(b.dataset.tpSport,b.dataset.tpId,b.dataset.tpName,b.dataset.tpLeague);}
  });
  function setup(tag){
    view="teamprofile";
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var d=document.getElementById("detail");if(d)d.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent=tag;
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.remove("active");});
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    if(typeof toggleMenu==="function")toggleMenu(false);
  }
  window.openTeamProfile=async function(sport,id,name,league){
    setup(name||"Team");
    var list=document.getElementById("list");
    list.innerHTML='<div class="tp-wrap"><div class="tp-loading">Loading '+esc(name||"team")+'\u2026</div></div>';
    var url="/api/team-profile?sport="+encodeURIComponent(sport)+"&team_id="+encodeURIComponent(id)+"&name="+encodeURIComponent(name||"");
    if(league)url+="&league="+encodeURIComponent(league);
    var d;try{d=await getJSON(url);}catch(e){
      list.innerHTML='<div class="tp-wrap"><div class="tp-empty">Couldn\u2019t load this profile right now.</div></div>';return;}
    renderTeamProfile(d);
  };
  function stat(k,v,sub){if(v==null||v==="")return "";return '<div class="tp-stat"><div class="tp-stat-v">'+esc(v)+'</div><div class="tp-stat-k">'+esc(k)+(sub?' <span>'+esc(sub)+'</span>':"")+'</div></div>';}
  function renderTeamProfile(d){
    var list=document.getElementById("list");
    if(!d||d.unsupported){list.innerHTML='<div class="tp-wrap"><div class="tp-empty">Team profiles for this sport are coming next \u2014 it runs on a different data provider.</div></div>';return;}
    if(d.error||!d.games){list.innerHTML='<div class="tp-wrap"><div class="tp-empty">No completed games on record yet for '+esc(d.name||"this team")+'.</div></div>';return;}
    var _fc={W:"w",L:"l",D:"d"};
    var formBits=(d.form||"").split("").map(function(c){return '<span class="tp-f '+(_fc[c]||"l")+'">'+c+'</span>';}).join("");
    var recent=(d.recent||[]).map(function(r){var rc=r.res||(r.won?"w":"l"),rl=rc.toUpperCase();
      return '<div class="tp-g"><span class="tp-g-res '+rc+'">'+rl+'</span>'+
        '<span class="tp-g-opp">'+(r.home?"vs":"@")+' '+esc(r.opp||"")+'</span>'+
        '<span class="tp-g-sc">'+esc(r.score||"")+'</span></div>';}).join("");
    var term=d.score_term||"Pts";
    list.innerHTML='<div class="tp-wrap">'+
      '<div class="tp-head">'+(d.badge?'<img class="tp-badge" src="'+esc(d.badge)+'" alt="">':"")+'<div class="tp-name">'+esc(d.name||"Team")+'</div>'+
        (d.rating!=null?'<div class="tp-rating">'+esc(d.rating)+'<span>power rating</span></div>':"")+'</div>'+
      '<div class="tp-stats">'+
        stat("Record",d.record,d.games+" gp")+stat("Last 10",d.last10)+stat("Streak",d.streak)+
        stat("Home",d.home_record)+stat("Away",d.away_record)+
        stat(term+" for",d.ppg,"per game")+stat(term+" against",d.opp_ppg,"per game")+'</div>'+
      (formBits?'<div class="tp-form"><div class="tp-lbl">Recent form</div><div class="tp-fbar">'+formBits+'</div></div>':"")+
      (recent?'<div class="tp-recent"><div class="tp-lbl">Last games</div>'+recent+'</div>':"")+
      (d.adv?('<div class="tp-adv"><div class="tp-lbl">This season \u00b7 advanced</div><div class="tp-stats">'+
        stat("Goals/game",d.adv.gf_avg)+stat("Conceded/game",d.adv.ga_avg)+
        stat("Clean sheets",d.adv.clean_sheets)+stat("Failed to score",d.adv.failed_to_score)+
        stat("Biggest win",d.adv.biggest_win)+stat("Biggest loss",d.adv.biggest_loss)+'</div>'+
        (d.adv.formations&&d.adv.formations.length?'<div class="tp-forms">Formations: '+d.adv.formations.map(esc).join(" \u00b7 ")+'</div>':"")+
        '<div class="tp-src">via api-football</div></div>'):"")+
      '<div class="tp-foot">Every figure here is computed from actual game results \u2014 no projected or fabricated ratings.</div></div>';
  }
})();
/* ===== New-visitor welcome hero (shows once) ===== */
(function(){
  try{if(localStorage.getItem("ll_seen_intro"))return;}catch(e){return;}
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  async function show(){
    var stat="";
    try{var d=await getJSON("/api/results/recent?days=7");if(d&&d.summary&&(d.summary.w+d.summary.l)>0)stat=d.summary.record;}catch(e){}
    var ov=document.createElement("div");ov.id="ll-hero";ov.className="hero-ov";
    ov.innerHTML='<div class="hero-card">'+
      '<img class="hero-logo" src="/icon-180.png" alt="Line Logic">'+
      '<div class="hero-h">Model sports predictions.<br>Every pick tracked.</div>'+
      '<div class="hero-p">A calibrated model across MLB, NBA, NFL, NHL, tennis, soccer &amp; college \u2014 graded in public. No deleted losers, no hype.</div>'+
      (stat?'<div class="hero-stat">Model went <b>'+esc(stat)+'</b> the last 7 days</div>':"")+
      '<div class="hero-btns"><button class="hero-btn" id="hero-results">See the track record</button>'+
      '<button class="hero-btn hero-alt" id="hero-go">Explore the board</button></div></div>';
    document.body.appendChild(ov);
    function close(){try{localStorage.setItem("ll_seen_intro","1");}catch(e){}if(ov.parentNode)ov.parentNode.removeChild(ov);}
    document.getElementById("hero-go").addEventListener("click",close);
    document.getElementById("hero-results").addEventListener("click",function(){close();if(typeof openResults==="function")openResults();});
    ov.addEventListener("click",function(e){if(e.target===ov)close();});
  }
  if(document.body)show();else document.addEventListener("DOMContentLoaded",show);
})();
