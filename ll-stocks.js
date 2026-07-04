/* ll-stocks.js — Line Logic "Markets" (paper-traded model signals + live quotes).
   SELF-CONTAINED & REMOVABLE. Educational / paper-traded only — NOT financial advice. */
(function(){
  var RANGES=["1D","1W","1M","3M","1Y","ALL"];
  var curRange="1D", quoteData=null, pfData=null, sigData=null;
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function money(n){return "$"+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0});}
  function pct(n){n=Number(n||0);return (n>=0?"+":"")+n.toFixed(2)+"%";}
  function fp(n){n=Number(n||0);return n<5?("$"+n.toFixed(4)):("$"+n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));}

  function setup(){
    view="stocks";
    document.getElementById("home").classList.remove("show");
    document.getElementById("main").style.display="";
    var d=document.getElementById("detail");if(d)d.style.display="none";
    var st=document.getElementById("sport-tag");if(st)st.textContent="Markets";
    document.querySelectorAll(".menu-item").forEach(function(it){it.classList.toggle("active",it.dataset.view==="stocks");});
    var sd=document.getElementById("side");if(sd)sd.style.display="none";
    var sh=document.querySelector(".shell");if(sh)sh.style.gridTemplateColumns="1fr";
    document.getElementById("tabs").innerHTML="";document.getElementById("bubbles").innerHTML="";
    var _db=document.getElementById("datebar");if(_db)_db.innerHTML="";
    var _ab=document.getElementById("acc-badge");if(_ab)_ab.style.display="none";
    var _tb=document.getElementById("today-badge");if(_tb)_tb.style.display="none";
    if(typeof toggleMenu==="function")toggleMenu(false);
    var ss=document.getElementById("slatesearch");if(ss)ss.remove();
  }

  function spark(series,up,w,h){
    w=w||120;h=h||36;
    if(!series||series.length<2)return '<svg class="sk-spark" width="'+w+'" height="'+h+'"></svg>';
    var mn=Math.min.apply(null,series),mx=Math.max.apply(null,series),rng=(mx-mn)||1,p=3;
    var pts=series.map(function(v,i){
      var x=p+(w-2*p)*i/(series.length-1),y=p+(h-2*p)*(1-(v-mn)/rng);
      return x.toFixed(1)+","+y.toFixed(1);}).join(" ");
    var col=up?"var(--accent)":"var(--loss)";
    return '<svg class="sk-spark" viewBox="0 0 '+w+' '+h+'" width="'+w+'" height="'+h+'" preserveAspectRatio="none">'+
      '<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.6"/></svg>';
  }

  function bigChart(series,up){
    var W=340,H=160,p=6;
    if(!series||series.length<2)return '<div class="sk-chart-empty">Chart builds as data comes in.</div>';
    var mn=Math.min.apply(null,series),mx=Math.max.apply(null,series),rng=(mx-mn)||1;
    var pts=series.map(function(v,i){
      var x=p+(W-2*p)*i/(series.length-1),y=p+(H-2*p)*(1-(v-mn)/rng);
      return x.toFixed(1)+","+y.toFixed(1);}).join(" ");
    var col=up?"var(--accent)":"var(--loss)";
    var last=series.map(function(v,i){var x=p+(W-2*p)*i/(series.length-1),y=p+(H-2*p)*(1-(v-mn)/rng);return[x,y];}).pop();
    return '<svg class="sk-big" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
      '<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2.2"/>'+
      '<circle cx="'+last[0].toFixed(1)+'" cy="'+last[1].toFixed(1)+'" r="3.2" fill="'+col+'"/></svg>';
  }

  function rangeTabs(){
    return '<div class="sk-tabs">'+RANGES.map(function(r){
      return '<button class="sk-tab'+(r===curRange?" on":"")+'" data-r="'+r+'">'+r+'</button>';}).join("")+'</div>';
  }

  function assetRow(a){
    var up=(a.change_pct||0)>=0;
    return '<div class="sk-row"><div class="sk-tk"><div class="sk-sym">'+esc(a.ticker.replace("-USD",""))+'</div>'+
      '<div class="sk-nm">'+esc(a.name||"")+'</div></div>'+
      '<div class="sk-spk">'+spark(a.series,up)+'</div>'+
      '<div class="sk-px"><div class="sk-price">'+fp(a.price)+'</div>'+
      '<div class="sk-chip '+(up?"up":"dn")+'">'+(up?"\u25B2":"\u25BC")+" "+Math.abs(a.change_pct).toFixed(2)+'%</div></div></div>';
  }

  function section(title,rows){
    if(!rows||!rows.length)return "";
    return '<div class="sk-sec">'+title+'</div><div class="sk-list">'+rows.map(assetRow).join("")+'</div>';
  }

  function sigRow(s){
    var sg=(s.signal||"hold");
    return '<div class="sk-row"><div class="sk-tk"><div class="sk-sym">'+esc(s.ticker)+'</div>'+
      '<div class="sk-nm">'+esc(s.reason||"")+'</div></div>'+
      '<div class="sk-px"><div class="sk-price">'+(s.price!=null?fp(s.price):"")+'</div>'+
      (s.rsi!=null?'<div class="sk-nm">RSI '+s.rsi+'</div>':"")+'</div>'+
      '<div class="sk-sig '+sg+'">'+sg.toUpperCase()+'</div></div>';
  }

  function renderQuotes(){
    var box=document.getElementById("sk-quotes");if(!box)return;
    if(!quoteData||!quoteData.groups){box.innerHTML='<div class="sk-loading">No quote data \u2014 check back when markets have data.</div>';return;}
    var g=quoteData.groups;
    box.innerHTML=section("Stocks",g.stocks)+section("ETFs",g.etfs)+section("Crypto",g.crypto);
  }

  async function loadQuotes(){
    var box=document.getElementById("sk-quotes");if(box)box.innerHTML='<div class="sk-loading">Loading '+curRange+' quotes\u2026</div>';
    try{quoteData=await getJSON("/api/stocks/quotes?range="+encodeURIComponent(curRange));}catch(e){quoteData=null;}
    renderQuotes();
  }

  window.openStocks=async function(){
    setup();
    var list=document.getElementById("list");
    list.innerHTML='<div class="sk-wrap"><div class="sk-loading">Loading Markets\u2026</div></div>';
    try{pfData=await getJSON("/api/stocks/portfolio");}catch(e){pfData={};}
    try{sigData=await getJSON("/api/stocks/signals");}catch(e){sigData={};}
    var pf=pfData||{},vs=(pf.vs_spy_pct!=null)?pf.vs_spy_pct:null;
    var eqUp=(pf.return_pct||0)>=0;
    var eqSeries=(pf.equity||[]).map(function(e){return e.value;});
    var h='<div class="sk-wrap">';
    h+='<div class="sk-search"><input id="sk-q" class="sk-qi" placeholder="Search stocks, ETFs, crypto\u2026" autocomplete="off" autocapitalize="characters"><div id="sk-res" class="sk-res"></div></div>';
    // paper portfolio card
    h+='<div class="sk-pf"><div class="sk-pf-k">Paper portfolio \u00b7 model</div>'+
      '<div class="sk-pf-v">'+money(pf.value||100000)+'</div>'+
      '<div class="sk-pf-sub"><span class="'+(eqUp?"up":"dn")+'">'+pct(pf.return_pct)+'</span>'+
      (vs!=null?' <span class="sk-vs '+((vs)>=0?"up":"dn")+'">'+pct(vs)+' vs SPY</span>':"")+'</div>'+
      bigChart(eqSeries,eqUp)+
      '<div class="sk-leg"><span><i class="sk-dot acc"></i>Model paper P/L</span>'+
      (pf.win_rate!=null?'<span>'+pf.closed_trades+' closed \u00b7 '+pf.win_rate+'% win</span>':"")+'</div></div>';
    // timeframe tabs + quotes
    h+=rangeTabs();
    h+='<div id="sk-quotes"><div class="sk-loading">Loading quotes\u2026</div></div>';
    // model signals
    var rows=(sigData&&sigData.signals)||[];
    if(rows.length){h+='<div class="sk-sec">Model signals (paper)</div><div class="sk-list">'+rows.map(sigRow).join("")+'</div>';}
    // footnote
    h+='<div class="sk-foot">'+esc(pf.disclaimer||(sigData&&sigData.disclaimer)||"Educational, paper-traded model signals \u2014 not financial advice. Consult a licensed advisor before investing.")+'</div>';
    h+='</div>';
    list.innerHTML=h;
    list.querySelectorAll(".sk-tab").forEach(function(b){b.addEventListener("click",function(){
      curRange=b.dataset.r;list.querySelectorAll(".sk-tab").forEach(function(x){x.classList.toggle("on",x===b);});loadQuotes();});});
    wireSearch();
    loadQuotes();
  };

  var _sT=null;
  function wireSearch(){
    var inp=document.getElementById("sk-q"),res=document.getElementById("sk-res");
    if(!inp)return;
    inp.addEventListener("input",function(){
      var q=inp.value.trim();
      if(_sT)clearTimeout(_sT);
      if(!q){res.innerHTML="";res.classList.remove("on");return;}
      _sT=setTimeout(async function(){
        try{var d=await getJSON("/api/stocks/search?q="+encodeURIComponent(q));
          var rows=(d&&d.results)||[];
          res.innerHTML=rows.map(function(r){
            return '<div class="sk-hit" data-s="'+esc(r.symbol)+'"><span class="sk-hit-s">'+esc(r.symbol.replace("-USD",""))+'</span>'+
              '<span class="sk-hit-n">'+esc(r.name||"")+'</span><span class="sk-hit-t">'+esc(r.type||"")+'</span></div>';}).join("");
          res.classList.add("on");
          res.querySelectorAll(".sk-hit").forEach(function(el){el.addEventListener("click",function(){openSymbol(el.dataset.s);});});
        }catch(e){}
      },220);
    });
  }

  var symRange="1D";
  async function openSymbol(sym){
    var list=document.getElementById("list");
    list.innerHTML='<div class="sk-wrap"><div class="sk-loading">Loading '+esc(sym)+'\u2026</div></div>';
    async function draw(){
      var d;try{d=await getJSON("/api/stocks/quote?symbol="+encodeURIComponent(sym)+"&range="+encodeURIComponent(symRange));}catch(e){d={error:"failed"};}
      var wrap=document.getElementById("list");
      if(!d||d.error){wrap.innerHTML='<div class="sk-wrap"><button class="sk-back" id="sk-bk">\u2190 Markets</button><div class="sk-loading">No data for '+esc(sym)+'.</div></div>';
        var b=document.getElementById("sk-bk");if(b)b.addEventListener("click",openStocks);return;}
      var up=(d.change_pct||0)>=0;
      var h='<div class="sk-wrap"><button class="sk-back" id="sk-bk">\u2190 Markets</button>'+
        '<div class="sk-detail"><div class="sk-d-sym">'+esc(d.symbol.replace("-USD",""))+'</div>'+
        '<div class="sk-d-nm">'+esc(d.name||"")+'</div>'+
        '<div class="sk-d-px">'+fp(d.price)+'</div>'+
        '<div class="sk-d-chg '+(up?"up":"dn")+'">'+(up?"\u25B2":"\u25BC")+" "+pct(d.change_pct)+' \u00b7 '+symRange+'</div>'+
        bigChart(d.series,up)+
        '<div class="sk-tabs" id="sk-dtabs">'+RANGES.map(function(r){return '<button class="sk-tab'+(r===symRange?" on":"")+'" data-r="'+r+'">'+r+'</button>';}).join("")+'</div>'+
        '<div class="sk-foot">'+esc(d.disclaimer||"Educational \u2014 not financial advice.")+'</div></div></div>';
      wrap.innerHTML=h;
      document.getElementById("sk-bk").addEventListener("click",openStocks);
      wrap.querySelectorAll("#sk-dtabs .sk-tab").forEach(function(b){b.addEventListener("click",function(){symRange=b.dataset.r;draw();});});
    }
    draw();
  }
})();
