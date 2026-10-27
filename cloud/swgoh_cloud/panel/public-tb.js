// Shared table presentation only. No private API routes or action credentials.
const tbOpenMembers=new Set();
let tbDeployAscending=false;
function tbRunLabel(p){const name=p.definition==='t05D'?'Rise of the Empire':p.definition;let start=p.battleStart,estimated=false;if(!start&&p.phaseEnd&&p.phase){start=Number(p.phaseEnd)-Number(p.phase)*86400000;estimated=true}const date=start?new Date(Number(start)):null;return name+' · početak: '+(date&&!Number.isNaN(date.getTime())?date.toLocaleDateString('sr-Latn')+(estimated?' (procena)':''):'nije zabeležen')+' · '+p.battleKey.slice(0,8)}
function tbSortedRows(rows){return [...rows].sort((a,b)=>{const av=a.deployedGP,bv=b.deployedGP;if(av==null)return bv==null?0:(tbDeployAscending?-1:1);if(bv==null)return tbDeployAscending?1:-1;const x=BigInt(av),y=BigInt(bv);return (x<y?-1:x>y?1:0)*(tbDeployAscending?1:-1)})}
function tbEsc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function tbNum(x){try{return x===null||x===undefined?'—':new Intl.NumberFormat('sr-Latn').format(BigInt(x))}catch{return '—'}}
function tbBool(x){return x===true?'Da':x===false?'Ne':'—'}
function tbMarkup(records,runKey,phase,all){
 const runs=[...new Map(records.map(r=>[r.payload.battleKey,{key:r.payload.battleKey,definition:r.payload.definition,label:tbRunLabel(r.payload)}])).values()];
 if(!runs.length)return '<p class="hint">Još nema TB snimka. Otvori gild i TB statistike u igri.</p>';
 runs.sort((a,b)=>{const start=k=>Math.max(0,...records.filter(r=>r.payload.battleKey===k).map(r=>Number(r.payload.battleStart)||Number(r.payload.phaseEnd)-r.payload.phase*86400000||0));return start(b.key)-start(a.key)||b.key.localeCompare(a.key)});
 const key=runs.some(r=>r.key===runKey)?runKey:runs[0].key;
 const selected=records.filter(r=>r.payload.battleKey===key);const active=phase||Math.max(...selected.map(r=>r.payload.phase));
 const p=selected.find(r=>r.payload.phase===active)?.payload;const phases=Array.from({length:6},(_,i)=>i+1);
 const states={live:'U toku',final:'Završni odgovor',closed_last_seen:'Zatvorena · poslednji snimak'};
 let html='<h2>Territory Battle · učinak članova</h2><label>TB događaj <select id="tb-run">'+runs.map(r=>'<option value="'+tbEsc(r.key)+'" '+(key===r.key?'selected':'')+'>'+tbEsc(r.label)+'</option>').join('')+'</select></label><div class="toolbar">'+phases.map(n=>{let rec=selected.find(r=>r.payload.phase===n)?.payload;return '<button data-tb-phase="'+n+'" '+(active===n?'class="chosen active"':'')+'>Faza '+n+' · '+(rec?tbEsc(states[rec.state]||rec.state):'Nema snimka')+'</button>'}).join('')+'</div>';
 if(!p)return html+'<p class="hint">Za ovu fazu nema sačuvanog odgovora. Buduće faze se ne popunjavaju nulama.</p>';
 html+='<p><strong>'+tbEsc(tbRunLabel(p))+'</strong></p><p>'+tbEsc(states[p.state]||p.state)+' · Statistike: '+tbEsc(p.statsObservedAt?new Date(p.statsObservedAt).toLocaleString():'nisu očitane')+' · '+p.rows.length+' članova</p><label><input type="checkbox" id="tb-all" '+(all?'checked':'')+'> Sve statističke kolone faze ('+p.columns.length+')</label>';
 if(p.truncated)html+='<p class="error">Odgovor je skraćen. Nedostajuće vrednosti nisu nule.</p>';
 const columns=all?['deployedGP',...p.columns]:['deployedGP','undeployedGP','territoryPoints','waves','combatAttempts','donatedUnits','played','deployed','donated'];
 const labels={territoryPoints:'Territory points',deployedGP:'Deployed GP',undeployedGP:'Undeployed GP*',waves:'Talasi',combatAttempts:'Borbeni pokušaji',donatedUnits:'Donirane jedinice',played:'Igrao misiju',deployed:'Rasporedio GP',donated:'Donirao'};
 html+='<div class="table-wrap" data-tb-scroll style="overflow:auto"><table><thead><tr><th>Član</th>'+columns.map(c=>'<th'+(c==='deployedGP'?' aria-sort="'+(tbDeployAscending?'ascending':'descending')+'"':'')+'>'+(c==='deployedGP'?'<button class="text-button" data-tb-sort="deployedGP">Deployed GP '+(tbDeployAscending?'↑':'↓')+'</button>':tbEsc(labels[c]||c))+'</th>').join('')+'<th>Svi podaci / misije</th></tr></thead><tbody>';
 for(const [rank,row] of tbSortedRows(p.rows).entries()){html+='<tr><td><strong>'+String(rank+1).padStart(2,'0')+' · '+tbEsc(row.name||'Član bez imena')+'</strong></td>'+columns.map(c=>'<td>'+tbEsc(['played','deployed','donated'].includes(c)?tbBool(row[c]):tbNum(all&&c!=='deployedGP'?row.values[c]:row[c]))+'</td>').join('');
 html+='<td><details data-tb-member="'+tbEsc(row.memberKey)+'" '+(tbOpenMembers.has(row.memberKey)?'open':'')+'><summary>Statistike i statusi ('+Object.keys(row.values).length+')</summary><p>GP u snimku: '+tbNum(row.galacticPower)+'</p><table><tr><th>Misija / zona</th><th>Server rezultat</th><th>Pokušano</th><th>Pređeno</th></tr>'+row.missions.map(m=>'<tr><td>'+tbEsc(m.zone)+'</td><td>'+tbEsc(m.result)+'</td><td>'+tbBool(m.attempted)+'</td><td>'+tbBool(m.completed)+'</td></tr>').join('')+'</table><table>'+p.columns.map(c=>'<tr><td>'+tbEsc(c)+'</td><td>'+tbNum(row.values[c])+'</td></tr>').join('')+'</table></details></td></tr>';}
 return html+'</tbody></table></div><p class="hint">— znači da podatak nije dostupan. Pređeno/Ne koristi eksplicitni rezultat specijalne misije; broj talasa ne dokazuje da je svaka borba završena. *Undeployed GP je izveden iz GP-a sačuvanog uz ovu fazu i deployed GP-a; može odstupati od zaključanog TB rostera. Završni odgovor označava istorijske podatke dobijene u sledećoj fazi ili rezultatu TB-a. Ako je kraj propušten, ostaje oznaka poslednjeg snimka.</p>';
}
document.addEventListener('toggle',e=>{const key=e.target?.dataset?.tbMember;if(key){if(e.target.open)tbOpenMembers.add(key);else tbOpenMembers.delete(key)}},true);

// Keep the TB horizontal scrollbar reachable while its table is on screen.
(()=>{
 const bar=document.createElement('div');bar.className='tb-bottom-scroll';bar.hidden=true;
 bar.setAttribute('role','region');bar.setAttribute('aria-label','Horizontalno pomeranje TB tabele');bar.tabIndex=0;
 const track=document.createElement('div');bar.append(track);document.body.append(bar);
 let table=null,frame=0,savedLeft=0;
 const observer=new ResizeObserver(schedule);
 function schedule(){if(!frame)frame=requestAnimationFrame(update)}
 function update(){
  frame=0;const next=document.querySelector('[data-tb-scroll]');
  if(next!==table){if(table){savedLeft=table.scrollLeft;table.removeEventListener('scroll',fromTable);observer.disconnect()}table=next;if(table){table.scrollLeft=savedLeft;table.addEventListener('scroll',fromTable,{passive:true});observer.observe(table);observer.observe(table.firstElementChild)}}
  if(!table){bar.hidden=true;return}
  const r=table.getBoundingClientRect(),width=Math.min(r.right,document.documentElement.clientWidth)-Math.max(0,r.left);
  bar.hidden=!(r.top<innerHeight-28&&r.bottom>28&&width>0&&table.scrollWidth>table.clientWidth+1);
  if(bar.hidden)return;
  bar.style.left=Math.max(0,r.left)+'px';bar.style.width=width+'px';track.style.width=(table.scrollWidth-table.clientWidth+width)+'px';bar.scrollLeft=table.scrollLeft;
 }
 function fromTable(){savedLeft=table.scrollLeft;bar.scrollLeft=savedLeft}
 bar.addEventListener('scroll',()=>{if(table&&Math.abs(table.scrollLeft-bar.scrollLeft)>1){table.scrollLeft=bar.scrollLeft;savedLeft=table.scrollLeft}},{passive:true});
 new MutationObserver(schedule).observe(document.querySelector('main')||document.body,{childList:true,subtree:true});
 addEventListener('scroll',schedule,{passive:true,capture:true});addEventListener('resize',schedule);schedule();
})();


let publicRecords=[],publicRun='',publicPhase=0,publicAll=false;
function publicRender(){document.querySelector('#public-table').innerHTML=tbMarkup(publicRecords,publicRun,publicPhase,publicAll);}
document.addEventListener('click',e=>{const phase=e.target.closest('[data-tb-phase]');if(phase){publicPhase=Number(phase.dataset.tbPhase);publicRender();}if(e.target.closest('[data-tb-sort]')){tbDeployAscending=!tbDeployAscending;publicRender();}});
document.addEventListener('change',e=>{if(e.target.id==='tb-run'){publicRun=e.target.value;publicPhase=0;publicRender();}if(e.target.id==='tb-all'){publicAll=e.target.checked;publicRender();}});
let publicBusy=false;
async function publicRefresh(){if(publicBusy)return;publicBusy=true;try{const response=await fetch('/public/tb/phases',{credentials:'omit',redirect:'error'});if(!response.ok)throw Error('Podaci trenutno nisu dostupni. Sačekaćemo sledeće osvežavanje.');publicRecords=await response.json();publicRender();document.querySelector('#public-error').hidden=true;const dates=publicRecords.map(r=>Date.parse(r.payload.statsObservedAt||r.payload.observedAt)).filter(Number.isFinite);document.querySelector('#public-status').textContent=dates.length?'Poslednji snimak: '+new Date(Math.max(...dates)).toLocaleString('sr-Latn'):'Još nema sačuvanih TB rezultata.';}catch(e){document.querySelector('#public-error').textContent=e.message;document.querySelector('#public-error').hidden=false;}finally{publicBusy=false;}}
publicRefresh();setInterval(publicRefresh,60000);
