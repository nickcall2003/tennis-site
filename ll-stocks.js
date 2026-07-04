/* ll-stocks.js — Line Logic "Markets": Robinhood-style multi-page section.
   Pages: Investing (home) · Explore (movers) · Search · Portfolio (paper).
   SELF-CONTAINED & REMOVABLE. Educational / paper-traded — NOT financial advice. */
(function(){
  var RANGES=["1D","1W","1M","3M","1Y","ALL"];
  var curRange="1D", page="home", _sT=null, symRange="1D";
  function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
  function money(n){return "$"+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:0});}
  function pct(n){n=Number(n||0);return (n>=0?"+":"")+n.toFixed(2)+"%";}
  function fp(n){n=Number(n||0);return n<5?("$"+n.toFixed(n<0.01?6:4)):("$"+n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));}
  function j(u){return getJSON(u);}

  function spark(series,up,w,h){
    w=w||130;h=h||34;
    if(!series||series.length<2)return '<svg width="'+w+'" height="'+h+'"></svg>';
    var mn=Math.min.apply(null,series),mx=Math.max.apply(null,series),rng=(mx-mn)||1,p=2;
    var pts=series.map(function(v,i){return (p+(w-2*p)*i/(series.length-1)).toFixed(1)+","+(p+(h-2*p)*(1-(v-mn)/rng)).toFixed(1);}).join(" ");
    return '<svg class="sk-spark" viewBox="0 0 '+w+' '+h+'" width="'+w+'" height="'+h+'" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="'+(up?"var(--accent)":"var(--loss)")+'" stroke-width="1.6"/></svg>';
  }
  function bigChart(series,up){
    var W=340,H=170,p=6;
    if(!series||series.length<2)return '<div class="sk-chart-empty">Chart builds as data comes in.</div>';
    var mn=Math.min.apply(null,series),mx=Math.max.apply(null,series),rng=(mx-mn)||1;
    var pts=series.map(function(v,i){return (p+(W-2*p)*i/(series.length-1)).toFixed(1)+","+(p+(H-2*p)*(1-(v-mn)/rng)).toFixed(1);}).join(" ");
    var lx=(W-p),ly=(p+(H-2*p)*(1-(series[series.length-1]-mn)/rng));
    var col=up?"var(--accent)":"var(--loss)";
    return '<svg class="sk-big" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2.2"/><circle cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="3.2" fill="'+col+'"/></svg>';
  }
  function rtabs(){return '<div class="sk-tabs" id="sk-rtabs">'+RANGES.map(function(r){return '<button class="sk-tab'+(r===curRange?" on":"")+'" data-r="'+r+'">'+r+'</button>';}).join("")+'</div>';}
  function wireRtabs(cb){document.querySelectorAll("#sk-rtabs .sk-tab").forEach(function(b){b.addEventListener("click",function(){curRange=b.dataset.r;document.querySelectorAll("#sk-rtabs .sk-tab").forEach(function(x){x.classList.toggle("on",x===b);});cb();});});}

  function row(a){
    var up=(a.change_pct||0)>=0;
    return '<div class="sk-row" data-sym="'+esc(a.ticker)+'"><div class="sk-tk"><div class="sk-sym">'+esc(a.ticker.replace("-USD",""))+'</div><div class="sk-nm">'+esc(a.name||"")+'</div></div>'+
      '<div class="sk-spk">'+spark(a.series,up)+'</div>'+
      '<div class="sk-px"><div class="sk-price">'+fp(a.price)+'</div><div class="sk-chip '+(up?"up":"dn")+'">'+(up?"\u25B2":"\u25BC")+" "+Math.abs(a.change_pct).toFixed(2)+'%</div></div></div>';
  }
  function wireRows(box){box.querySelectorAll(".sk-row[data-sym]").forEach(function(el){el.addEventListener("click",function(){openSymbol(el.dataset.sym);});});}
  function moverCard(a){var up=(a.change_pct||0)>=0;return '<div class="sk-mv" data-sym="'+esc(a.ticker)+'"><div class="sk-mv-nm">'+esc(a.name||a.ticker)+'</div><div class="sk-mv-sym">'+esc(a.ticker.replace("-USD",""))+'</div>'+spark(a.series,up,150,44)+'<div class="sk-mv-px">'+fp(a.price)+'</div><div class="sk-chip '+(up?"up":"dn")+'">'+(up?"\u25B2":"\u25BC")+" "+Math.abs(a.change_pct).toFixed(2)+'%</div></div>';}

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
  var DISC='<div class="sk-foot">Educational, paper-traded model signals \u2014 not financial advice. No positions are real. Consult a licensed advisor before investing.</div>';

  /* ---- Investing (home) ---- */
  async function renderHome(){
    var box=document.getElementById("sk-page");
    box.innerHTML='<div class="sk-loading">Loading\u2026</div>';
    var pf={};try{pf=await j("/api/stocks/portfolio");}catch(e){}
    var eqUp=(pf.return_pct||0)>=0,vs=(pf.vs_spy_pct!=null)?pf.vs_spy_pct:null;
    var eqSeries=(pf.equity||[]).map(function(e){return e.value;});
    var h='<div class="sk-pf"><div class="sk-pf-k">Paper portfolio \u00b7 model (not real money)</div>'+
      '<div class="sk-pf-v">'+money(pf.value||100000)+'</div>'+
      '<div class="sk-pf-sub"><span class="'+(eqUp?"up":"dn")+'">'+pct(pf.return_pct)+'</span>'+(vs!=null?' <span class="sk-vs '+(vs>=0?"up":"dn")+'">'+pct(vs)+' vs SPY</span>':"")+'</div>'+
      '<div class="sk-pf-note">Hypothetical $100k start, paper-traded to test the model vs. holding SPY.</div>'+
      bigChart(eqSeries,eqUp)+'</div>';
    h+='<div class="sk-sec">Stocks &amp; ETFs</div>'+rtabs()+'<div id="sk-grid"><div class="sk-loading">Loading quotes\u2026</div></div>'+DISC;
    box.innerHTML=h;
    wireRtabs(loadGrid);loadGrid();
  }
  async function loadGrid(){
    var g=document.getElementById("sk-grid");if(!g)return;g.innerHTML='<div class="sk-loading">Loading '+curRange+'\u2026</div>';
    var d;try{d=await j("/api/stocks/quotes?range="+curRange);}catch(e){}
    if(!d||!d.groups){g.innerHTML='<div class="sk-loading">No quote data.</div>';return;}
    var gr=d.groups,html="";
    ["stocks","etfs","crypto"].forEach(function(k){(gr[k]||[]).forEach(function(a){html+=row(a);});});
    g.innerHTML=html||'<div class="sk-loading">No quotes.</div>';wireRows(g);
  }

  /* ---- Explore (movers) ---- */
  async function renderExplore(){
    var box=document.getElementById("sk-page");
    box.innerHTML='<div class="sk-sec">Top movers</div>'+rtabs()+'<div id="sk-mv"><div class="sk-loading">Loading movers\u2026</div></div>'+DISC;
    wireRtabs(loadMovers);loadMovers();
  }
  async function loadMovers(){
    var m=document.getElementById("sk-mv");if(!m)return;m.innerHTML='<div class="sk-loading">Loading '+curRange+'\u2026</div>';
    var d;try{d=await j("/api/stocks/movers?range="+curRange);}catch(e){}
    if(!d){m.innerHTML='<div class="sk-loading">No data.</div>';return;}
    var h="";
    if((d.gainers||[]).length){h+='<div class="sk-subsec up">\u25B2 Gainers</div><div class="sk-mvrow">'+d.gainers.map(moverCard).join("")+'</div>';}
    if((d.losers||[]).length){h+='<div class="sk-subsec dn">\u25BC Losers</div><div class="sk-mvrow">'+d.losers.map(moverCard).join("")+'</div>';}
    m.innerHTML=h||'<div class="sk-loading">No movers.</div>';
    m.querySelectorAll(".sk-mv[data-sym]").forEach(function(el){el.addEventListener("click",function(){openSymbol(el.dataset.sym);});});
  }

  /* ---- Search ---- */
  function renderSearch(){
    var box=document.getElementById("sk-page");
    box.innerHTML='<div class="sk-search"><input id="sk-q" class="sk-qi" placeholder="Search stocks, ETFs, crypto\u2026" autocomplete="off" autocapitalize="characters"></div><div id="sk-res2" class="sk-res2"><div class="sk-loading">Type a name or ticker \u2014 AAPL, Tesla, BTC\u2026</div></div>';
    var inp=document.getElementById("sk-q"),res=document.getElementById("sk-res2");
    inp.focus();
    inp.addEventListener("input",function(){
      var q=inp.value.trim();if(_sT)clearTimeout(_sT);
      if(!q){res.innerHTML='<div class="sk-loading">Type a name or ticker\u2026</div>';return;}
      res.innerHTML='<div class="sk-loading">Searching\u2026</div>';
      _sT=setTimeout(async function(){
        try{var d=await j("/api/stocks/search?q="+encodeURIComponent(q));var rows=(d&&d.results)||[];
          if(!rows.length){res.innerHTML='<div class="sk-loading">No matches.</div>';return;}
          res.innerHTML=rows.map(function(r){return '<div class="sk-hit" data-s="'+esc(r.symbol)+'"><span class="sk-hit-s">'+esc(r.symbol.replace("-USD",""))+'</span><span class="sk-hit-n">'+esc(r.name||"")+'</span><span class="sk-hit-t">'+esc(r.type||"")+'</span></div>';}).join("");
          res.querySelectorAll(".sk-hit").forEach(function(el){el.addEventListener("click",function(){openSymbol(el.dataset.s);});});
        }catch(e){res.innerHTML='<div class="sk-loading">Search unavailable right now.</div>';}
      },220);
    });
  }

  /* ---- Portfolio (paper) ---- */
  async function renderPortfolio(){
    var box=document.getElementById("sk-page");box.innerHTML='<div class="sk-loading">Loading\u2026</div>';
    var pf={};try{pf=await j("/api/stocks/portfolio");}catch(e){}
    var who=(typeof authUser!=="undefined"&&authUser)?authUser:null;
    var eqUp=(pf.return_pct||0)>=0,vs=(pf.vs_spy_pct!=null)?pf.vs_spy_pct:null;
    var h='<div class="sk-pf">'+(who?'<div class="sk-pf-k">'+esc(who)+' \u00b7 model paper portfolio</div>':'<div class="sk-pf-k">Model paper portfolio (not real money)</div>')+
      '<div class="sk-pf-v">'+money(pf.value||100000)+'</div>'+
      '<div class="sk-pf-sub"><span class="'+(eqUp?"up":"dn")+'">'+pct(pf.return_pct)+'</span>'+(vs!=null?' <span class="sk-vs '+(vs>=0?"up":"dn")+'">'+pct(vs)+' vs SPY</span>':"")+'</div>'+
      bigChart((pf.equity||[]).map(function(e){return e.value;}),eqUp)+
      (pf.win_rate!=null?'<div class="sk-stats">'+pf.closed_trades+' closed \u00b7 '+pf.win_rate+'% win rate</div>':"")+'</div>';
    var pos=(pf.positions||[]);
    h+='<div class="sk-sec">Holdings (paper)</div>';
    if(!pos.length){h+='<div class="sk-loading">No open positions. The model buys when its signals fire \u2014 holdings show up here.</div>';}
    else{h+='<div id="sk-pos">'+pos.map(function(p){return '<div class="sk-row" data-sym="'+esc(p.ticker)+'"><div class="sk-tk"><div class="sk-sym">'+esc(p.ticker)+'</div><div class="sk-nm">entry $'+Number(p.entry).toFixed(2)+'</div></div><div class="sk-px"><div class="sk-price">'+Number(p.shares).toFixed(2)+' sh</div></div></div>';}).join("")+'</div>';}
    h+=DISC;box.innerHTML=h;
    var pb=document.getElementById("sk-pos");if(pb)wireRows(pb);
  }

  /* ---- symbol detail ---- */
  async function openSymbol(sym){
    var box=document.getElementById("sk-page");box.innerHTML='<div class="sk-loading">Loading '+esc(sym)+'\u2026</div>';
    async function draw(){
      var d;try{d=await j("/api/stocks/quote?symbol="+encodeURIComponent(sym)+"&range="+symRange);}catch(e){d={error:1};}
      if(!d||d.error){box.innerHTML='<button class="sk-back" id="sk-bk">\u2190 Back</button><div class="sk-loading">No price data for '+esc(sym)+'.</div>';document.getElementById("sk-bk").addEventListener("click",render);return;}
      var up=(d.change_pct||0)>=0;
      box.innerHTML='<button class="sk-back" id="sk-bk">\u2190 Back</button><div class="sk-detail"><div class="sk-d-sym">'+esc(d.symbol.replace("-USD",""))+'</div><div class="sk-d-nm">'+esc(d.name||"")+'</div><div class="sk-d-px">'+fp(d.price)+'</div><div class="sk-d-chg '+(up?"up":"dn")+'">'+(up?"\u25B2":"\u25BC")+" "+pct(d.change_pct)+' \u00b7 '+symRange+'</div>'+bigChart(d.series,up)+'<div class="sk-tabs" id="sk-dt">'+RANGES.map(function(r){return '<button class="sk-tab'+(r===symRange?" on":"")+'" data-r="'+r+'">'+r+'</button>';}).join("")+'</div></div>'+DISC;
      document.getElementById("sk-bk").addEventListener("click",render);
      box.querySelectorAll("#sk-dt .sk-tab").forEach(function(b){b.addEventListener("click",function(){symRange=b.dataset.r;draw();});});
    }
    draw();
  }

  /* ---- nav + shell ---- */
  var PAGES={home:renderHome,explore:renderExplore,search:renderSearch,portfolio:renderPortfolio};
  function render(){var f=PAGES[page]||renderHome;f();}
  function nav(){
    var items=[["home","\uD83D\uDCC8","Investing"],["explore","\uD83E\uDDED","Explore"],["search","\uD83D\uDD0D","Search"],["portfolio","\uD83D\uDC64","Portfolio"]];
    return '<div class="sk-nav">'+items.map(function(it){return '<button class="sk-navb'+(it[0]===page?" on":"")+'" data-p="'+it[0]+'"><span class="sk-navi">'+it[1]+'</span><span class="sk-navl">'+it[2]+'</span></button>';}).join("")+'</div>';
  }
  window.openStocks=function(){
    setup();
    page="home";
    var list=document.getElementById("list");
    list.innerHTML='<div class="sk-app"><div id="sk-page" class="sk-page"></div>'+nav()+'</div>';
    list.querySelectorAll(".sk-navb").forEach(function(b){b.addEventListener("click",function(){
      page=b.dataset.p;list.querySelectorAll(".sk-navb").forEach(function(x){x.classList.toggle("on",x===b);});render();});});
    render();
  };
})();
