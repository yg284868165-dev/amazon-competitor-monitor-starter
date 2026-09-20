const labels={
  projects:{title:'产品项目',desc:'定义每个在售产品的监控空间和美国邮编；修改项目 ID 后，关联配置会自动同步',fields:{enabled:'启用',project_id:'项目 ID',project_name:'项目名称',postal_code:'美国邮编'}},
  competitors:{title:'竞品 ASIN',desc:'同一项目下的 ASIN 自动参与该项目全部关键词监控',fields:{enabled:'启用',project_id:'项目 ID',asin:'ASIN',internal_name:'内部名称',brand:'品牌（选填）',remark:'备注'}},
  keywords:{title:'关键词',desc:'设置每个项目需要监控的搜索词和页数',fields:{enabled:'启用',project_id:'项目 ID',keyword:'关键词',pages:'搜索页数'}},
  categories:{title:'BSR 类目',desc:'每个项目可配置一个或多个 Best Sellers 榜单；最大排名可设1–100，系统会自动调整采集页数和完整性门槛',fields:{enabled:'启用',project_id:'项目 ID',category_name:'类目名称',category_url:'Best Sellers 链接',max_rank:'最大排名'}}
};
const state={configs:{},headers:{},dirtyConfigs:new Set(),projects:[],competitorProjectFilter:'all',competitorEnabledFilter:'all',keywordProjectFilter:'all',keywordEnabledFilter:'all',newProductRows:[],newProductRules:{},runIsRunning:false,asinHistory:null,historyLoadedKey:''};
let runPollTimer=null,runPollGraceUntil=0;
const $=s=>document.querySelector(s);
const $$=s=>[...document.querySelectorAll(s)];
async function api(url,options={}){let r;try{r=await fetch(url,{headers:{'Content-Type':'application/json'},...options})}catch(e){throw new Error('无法连接本地服务，请重新打开 Amazon竞品监控 App；也可双击 start_web.command 启动')}let d;try{d=await r.json()}catch(e){throw new Error('本地服务返回了无法识别的响应，请查看 output/logs/web-ui.log')}if(!r.ok||d.ok===false)throw new Error(d.error||d.output||'请求失败');return d}
function toast(text,bad=false){const el=$('#toast');el.textContent=text;el.style.background=bad?'#9e342d':'#17221d';el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2600)}
function selectOptions(el,includeAll=true){el.innerHTML=(includeAll?'<option value="all">全部项目</option>':'')+state.projects.filter(x=>Number(x.enabled)).map(x=>`<option value="${escapeHtml(x.project_id)}">${escapeHtml(x.project_name)} (${escapeHtml(x.project_id)})</option>`).join('')}
function escapeHtml(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
async function loadConfig(kind){const data=await api('/api/config/'+kind);state.configs[kind]=data.rows;state.headers[kind]=data.headers;if(kind==='projects'){state.projects=data.rows;selectOptions($('#runProject'));selectOptions($('#scheduleProject'));populateHistoryProjects()}renderConfig(kind,data.headers,data.rows);if(kind==='competitors')populateHistoryAsins();clearConfigDirty(kind);updateCounts()}
function updateRowBadge(panel){const badge=panel.querySelector('.config-head .badge');if(!badge)return;const all=panel.querySelectorAll('tbody tr').length,visible=[...panel.querySelectorAll('tbody tr')].filter(tr=>!tr.hidden).length;badge.textContent=visible===all?all+' 条':`显示 ${visible} / 共 ${all} 条`}
function markConfigDirty(kind){if(kind!=='competitors')return;state.dirtyConfigs.add(kind);const button=$('#'+kind)?.querySelector('.save-table');if(button){button.textContent='保存修改（有未保存内容）';button.classList.add('unsaved')}}
function clearConfigDirty(kind){state.dirtyConfigs.delete(kind);const button=$('#'+kind)?.querySelector('.save-table');if(button){button.textContent='保存修改';button.classList.remove('unsaved')}}
function applyTableFilter(panel,kind){const projectFilter=panel.querySelector('.project-header-filter'),enabledFilter=panel.querySelector('.enabled-header-filter');if(!projectFilter||!enabledFilter)return;const prefix=kind==='competitors'?'competitor':'keyword';state[prefix+'ProjectFilter']=projectFilter.value;state[prefix+'EnabledFilter']=enabledFilter.value;projectFilter.classList.toggle('filtered',projectFilter.value!=='all');enabledFilter.classList.toggle('filtered',enabledFilter.value!=='all');panel.querySelectorAll('tbody tr').forEach(tr=>{const project=tr.querySelector('[data-field="project_id"]')?.value.trim(),enabled=tr.querySelector('input[data-field="enabled"]')?.checked?'enabled':'disabled';tr.hidden=(projectFilter.value!=='all'&&project!==projectFilter.value)||(enabledFilter.value!=='all'&&enabled!==enabledFilter.value)});updateRowBadge(panel)}
function renderConfig(kind,headers,rows){
  const meta=labels[kind],panel=$('#'+kind),filterable=['competitors','keywords'].includes(kind);
  const projectOptions=state.projects.filter(x=>Number(x.enabled)).map(x=>`<option value="${escapeHtml(x.project_id)}">${escapeHtml(x.project_name)} (${escapeHtml(x.project_id)})</option>`).join('');
  let bulk='';
  if(kind==='competitors')bulk=`<div class="bulk-box"><div><h3>批量添加 ASIN</h3><p>支持换行、空格、逗号或分号分隔；自动去重并直接保存。</p></div><div class="bulk-grid"><label>产品项目<select class="bulk-project">${projectOptions}</select></label><label>统一备注（选填）<input class="bulk-remark" value=""></label><label class="bulk-asins">ASIN 列表<textarea rows="5" placeholder="B0XXXXXXXX&#10;B0YYYYYYYY&#10;B0ZZZZZZZZ"></textarea></label><button class="primary bulk-submit">批量添加并保存</button></div><div class="bulk-result"></div></div>`;
  if(kind==='keywords')bulk=`<div class="bulk-box"><div><h3>批量添加关键词</h3><p>每行填写一个关键词，也支持用逗号或分号分隔；自动去重并直接保存。</p></div><div class="bulk-grid"><label>产品项目<select class="bulk-project">${projectOptions}</select></label><label>统一搜索页数（1–10）<input class="bulk-pages" type="number" min="1" max="10" value="3"></label><label class="bulk-keywords">关键词列表<textarea rows="5" placeholder="your main keyword&#10;your product keyword&#10;another search term"></textarea></label><button class="primary bulk-submit">批量添加并保存</button></div><div class="bulk-result"></div></div>`;
  const headerHtml=headers.map(h=>{
    if(filterable&&h==='enabled')return `<th><span class="filter-heading">${meta.fields[h]}<select class="header-filter enabled-header-filter" title="按启用状态筛选" aria-label="按启用状态筛选"><option value="all">全部</option><option value="enabled">已启用</option><option value="disabled">未启用</option></select></span></th>`;
    if(filterable&&h==='project_id')return `<th><span class="filter-heading">${meta.fields[h]}<select class="header-filter project-header-filter" title="按项目 ID 筛选" aria-label="按项目 ID 筛选"><option value="all">全部项目</option>${projectOptions}</select></span></th>`;
    return `<th>${meta.fields[h]||h}</th>`;
  }).join('');
  panel.innerHTML=`<article class="surface"><div class="config-head"><div><h2>${meta.title}</h2><p>${meta.desc}${kind==='projects'?'<br>项目 ID 仅使用小写英文、数字、下划线或短横线。':''}</p></div><span class="badge muted">${rows.length} 条</span></div>${bulk}<div class="table-wrap"><table class="config-table"><thead><tr>${headerHtml}<th>操作</th></tr></thead><tbody></tbody></table></div><div class="config-actions"><button class="add-row">添加一行</button><button class="primary save-table">保存修改</button></div></article>`;
  const body=panel.querySelector('tbody');
  rows.forEach(r=>body.appendChild(rowElement(kind,headers,r,panel)));
  panel.querySelector('.add-row').onclick=()=>{const row={enabled:1};if(filterable){const projectFilter=panel.querySelector('.project-header-filter'),enabledFilter=panel.querySelector('.enabled-header-filter');projectFilter.value='all';enabledFilter.value='all'}body.appendChild(rowElement(kind,headers,row,panel));markConfigDirty(kind);filterable?applyTableFilter(panel,kind):updateRowBadge(panel)};
  panel.querySelector('.save-table').onclick=()=>saveTable(kind,headers,panel);
  if(kind==='competitors')panel.querySelector('.bulk-submit').onclick=()=>bulkAddCompetitors(panel);
  if(kind==='keywords')panel.querySelector('.bulk-submit').onclick=()=>bulkAddKeywords(panel);
  if(filterable){const prefix=kind==='competitors'?'competitor':'keyword',projectFilter=panel.querySelector('.project-header-filter'),enabledFilter=panel.querySelector('.enabled-header-filter');if([...projectFilter.options].some(x=>x.value===state[prefix+'ProjectFilter']))projectFilter.value=state[prefix+'ProjectFilter'];enabledFilter.value=state[prefix+'EnabledFilter'];projectFilter.onchange=enabledFilter.onchange=()=>applyTableFilter(panel,kind);applyTableFilter(panel,kind)}
}
function rowElement(kind,headers,row,panel){
  const filterable=['competitors','keywords'].includes(kind),tr=document.createElement('tr');
  headers.forEach(h=>{
    const td=document.createElement('td');
    let field;
    if(h==='project_id'&&kind!=='projects'&&!row[h]){
      field=document.createElement('select');
      field.innerHTML='<option value="">请选择项目</option>'+state.projects.filter(x=>Number(x.enabled)).map(x=>`<option value="${escapeHtml(x.project_id)}">${escapeHtml(x.project_name)} (${escapeHtml(x.project_id)})</option>`).join('');
      if(filterable&&panel)field.addEventListener('change',()=>applyTableFilter(panel,kind));
    }else{
      field=document.createElement('input');
      if(h==='enabled'){field.type='checkbox';field.checked=Number(row[h])===1||row[h]===true;if(filterable&&panel)field.addEventListener('change',()=>applyTableFilter(panel,kind))}
      else{field.value=row[h]??'';if(['pages','max_rank'].includes(h))field.type='number';if(h==='max_rank'){field.min='1';field.max='100'}if(h==='project_id'){if(kind==='projects'){field.placeholder='例如 my_product';field.dataset.originalValue=row[h]??'';field.addEventListener('blur',()=>field.value=field.value.trim().toLowerCase().replace(/\s+/g,'_').replace(/[^a-z0-9_-]/g,''))}else{field.readOnly=true;field.title='项目归属只能在产品项目中统一修改'}}}
    }
    field.dataset.field=h;if(kind==='competitors'){field.addEventListener(field.type==='checkbox'||field.tagName==='SELECT'?'change':'input',()=>markConfigDirty(kind))}td.appendChild(field);tr.appendChild(td);
  });
  const td=document.createElement('td'),actions=document.createElement('div');actions.className='table-actions';
  if(kind==='competitors'){
    const historyButton=document.createElement('button');historyButton.textContent='查看档案';historyButton.className='history-row-button';historyButton.onclick=()=>openHistoryFromRow(tr);actions.appendChild(historyButton);
  }
  const btn=document.createElement('button');btn.textContent='删除';btn.className='delete-row';btn.onclick=()=>{tr.remove();markConfigDirty(kind);if(panel)updateRowBadge(panel)};actions.appendChild(btn);td.appendChild(actions);tr.appendChild(td);return tr;
}
async function saveTable(kind,headers,panel){const rows=[...panel.querySelectorAll('tbody tr')].map(tr=>{const row={};tr.querySelectorAll('[data-field]').forEach(i=>{row[i.dataset.field]=i.type==='checkbox'?(i.checked?1:0):i.value;if(kind==='projects'&&i.dataset.field==='project_id')row._original_project_id=i.dataset.originalValue||''});return row});try{await api('/api/config/'+kind,{method:'PUT',body:JSON.stringify({rows})});clearConfigDirty(kind);toast(kind==='projects'?'项目及关联配置已保存':'配置已保存');await loadConfig('projects');await Promise.all(Object.keys(labels).filter(x=>x!=='projects').map(loadConfig));return true}catch(e){toast(e.message,true);return false}}
async function bulkAddCompetitors(panel){const payload={project_id:panel.querySelector('.bulk-project').value,asins:panel.querySelector('.bulk-asins textarea').value,remark:panel.querySelector('.bulk-remark').value};if(state.dirtyConfigs.has('competitors')){if(!confirm('竞品 ASIN 表格有未保存的修改。是否先保存，再继续批量添加？'))return;if(!await saveTable('competitors',state.headers.competitors,$('#competitors')))return}panel=$('#competitors');const button=panel.querySelector('.bulk-submit'),result=panel.querySelector('.bulk-result');button.disabled=true;try{const d=await api('/api/competitors/bulk',{method:'POST',body:JSON.stringify(payload)});const parts=[`成功新增 ${d.added.length} 个`];if(d.duplicates.length)parts.push(`跳过重复 ${d.duplicates.length} 个：${d.duplicates.join(', ')}`);if(d.invalid.length)parts.push(`无效 ${d.invalid.length} 个：${d.invalid.join(', ')}`);result.textContent=parts.join('；');result.className='bulk-result '+(d.invalid.length?'warning':'success');panel.querySelector('.bulk-asins textarea').value='';toast(`已新增 ${d.added.length} 个竞品`);await loadConfig('competitors')}catch(e){result.textContent=e.message;result.className='bulk-result error';toast(e.message,true)}finally{button.disabled=false}}
async function bulkAddKeywords(panel){const button=panel.querySelector('.bulk-submit'),result=panel.querySelector('.bulk-result'),textarea=panel.querySelector('.bulk-keywords textarea');button.disabled=true;try{const d=await api('/api/keywords/bulk',{method:'POST',body:JSON.stringify({project_id:panel.querySelector('.bulk-project').value,keywords:textarea.value,pages:panel.querySelector('.bulk-pages').value})});const parts=[`成功新增 ${d.added.length} 个`];if(d.duplicates.length)parts.push(`跳过重复 ${d.duplicates.length} 个：${d.duplicates.join(', ')}`);result.textContent=parts.join('；');result.className='bulk-result '+(d.duplicates.length?'warning':'success');textarea.value='';toast(`已新增 ${d.added.length} 个关键词`);await loadConfig('keywords')}catch(e){result.textContent=e.message;result.className='bulk-result error';toast(e.message,true)}finally{button.disabled=false}}
function updateCounts(){$('#projectCount').textContent=(state.configs.projects||[]).filter(x=>Number(x.enabled)).length;$('#asinCount').textContent=(state.configs.competitors||[]).filter(x=>Number(x.enabled)).length;$('#keywordCount').textContent=(state.configs.keywords||[]).filter(x=>Number(x.enabled)).length}
function populateHistoryProjects(){
  const select=$('#historyProject');if(!select)return;
  const current=select.value;
  select.innerHTML=state.projects.filter(x=>Number(x.enabled)).map(x=>`<option value="${escapeHtml(x.project_id)}">${escapeHtml(x.project_name)} (${escapeHtml(x.project_id)})</option>`).join('');
  if([...select.options].some(x=>x.value===current))select.value=current;
  populateHistoryAsins()
}
function populateHistoryAsins(preferred=''){
  const project=$('#historyProject'),select=$('#historyAsin');if(!project||!select)return;
  const current=preferred||select.value;
  const rows=(state.configs.competitors||[]).filter(x=>x.project_id===project.value);
  select.innerHTML=rows.map(x=>{const name=x.internal_name||x.brand||x.asin;const disabled=Number(x.enabled)?'':'（已停用）';return `<option value="${escapeHtml(String(x.asin||'').toUpperCase())}">${escapeHtml(name)}｜${escapeHtml(String(x.asin||'').toUpperCase())}${disabled}</option>`}).join('');
  if([...select.options].some(x=>x.value===current))select.value=current;
  if(!rows.length)select.innerHTML='<option value="">该项目暂无竞品 ASIN</option>';
  updateHistoryLinks()
}
function updateHistoryLinks(){
  const project=$('#historyProject')?.value||'',asin=$('#historyAsin')?.value||'',days=$('#historyDays')?.value||'30';
  const amazon=$('#historyAmazonLink'),excel=$('#historyExcelLink');
  if(asin){amazon.href='https://www.amazon.com/dp/'+encodeURIComponent(asin);amazon.hidden=false;excel.href=`/reports/asin-history?project_id=${encodeURIComponent(project)}&asin=${encodeURIComponent(asin)}&days=${encodeURIComponent(days)}`;excel.hidden=false}else{amazon.hidden=true;excel.hidden=true}
}
function invalidateHistoryView(){state.historyLoadedKey='';$('#historyResults').hidden=true;$('#historyEmpty').hidden=false;$('#historyEmpty').textContent='点击“查看档案”整理所选 ASIN 的历史动作。';updateHistoryLinks()}
async function openHistoryFromRow(row){
  const project=row.querySelector('[data-field="project_id"]')?.value.trim(),asin=row.querySelector('[data-field="asin"]')?.value.trim().toUpperCase();
  if(!project||!asin){toast('请先填写项目 ID 和 ASIN',true);return}
  if(state.dirtyConfigs.has('competitors')){
    if(!confirm('竞品 ASIN 表格有未保存的修改。是否先保存，再查看动作档案？'))return;
    if(!await saveTable('competitors',state.headers.competitors,$('#competitors')))return
  }
  $('#historyProject').value=project;populateHistoryAsins(asin);$('#historyAsin').value=asin;updateHistoryLinks();
  await activateTab($('#tabs button[data-tab="asinHistory"]'))
}
function displayTime(value){return value?String(value).replace('T',' '):'—'}
function safeHttpUrl(value){try{const url=new URL(String(value));return ['http:','https:'].includes(url.protocol)?url.href:''}catch(e){return ''}}
function money(value){return value===null||value===undefined||value===''?'—':'$'+Number(value).toFixed(2).replace(/\.00$/,'')}
function historyValue(value,type){
  if(type==='images'){
    const images=(Array.isArray(value)?value:[]).map(safeHttpUrl).filter(Boolean);
    return images.length?`<div class="history-images">${images.map((url,index)=>`<a href="${escapeHtml(url)}" target="_blank" rel="noopener"><img src="${escapeHtml(url)}" alt="商品图 ${index+1}" loading="lazy"><span>图 ${index+1}</span></a>`).join('')}</div>`:'<span class="muted-text">无</span>'
  }
  if(type==='list'){
    const values=Array.isArray(value)?value:[];
    return values.length?`<ol class="history-list-value">${values.map(item=>`<li>${escapeHtml(item)}</li>`).join('')}</ol>`:'<span class="muted-text">无</span>'
  }
  return `<div class="history-text-value">${escapeHtml(value||'无')}</div>`
}
function renderHistoryActions(){
  const container=$('#historyActions'),filter=$('#historyCategory').value,data=state.asinHistory;
  if(!data)return;
  const actions=data.actions.filter(x=>filter==='all'||x.categories.includes(filter));
  container.innerHTML=actions.length?actions.map(action=>{const impacts=action.impacts||[];const evidence=impacts.length?`<details class="history-action-impacts"><summary>后续排名对照</summary><div class="history-impact-content">${impacts.map(row=>`<div class="history-impact-row"><div><strong>${escapeHtml(row.metric_label)}</strong><span>${escapeHtml(row.context)}</span></div>${[['before','动作前'],['day_1','1天后'],['day_3','3天后'],['day_7','7天后']].map(([key,label])=>`<div class="rank-point"><span>${label}</span>${rankPoint(row.points[key])}</div>`).join('')}</div>`).join('')}</div></details>`:'';return `<article class="history-action ${action.is_direct_action?'direct':'platform'}"><div class="history-action-head"><div><time>${escapeHtml(displayTime(action.observed_at))}</time><h3>${escapeHtml(action.title)}</h3></div><div class="history-tags">${action.category_labels.map(label=>`<span>${escapeHtml(label)}</span>`).join('')}</div></div><ul>${action.items.map(item=>`<li><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.summary)}</span></li>`).join('')}</ul>${evidence}</article>`}).join(''):'<div class="empty-state">所选时间内没有这类动作。</div>'
}
function renderHistoryListing(){
  const changes=state.asinHistory.listing_changes,container=$('#historyListingChanges');
  container.innerHTML=changes.length?changes.map(change=>{const item=change.item;return `<article class="listing-change"><div class="listing-change-head"><div><time>${escapeHtml(displayTime(change.observed_at))}</time><h3>${escapeHtml(item.label)}</h3></div><span>${escapeHtml(item.summary)}</span></div><div class="listing-compare"><div><h4>变化前</h4>${historyValue(item.old,item.display_type)}</div><div><h4>变化后</h4>${historyValue(item.new,item.display_type)}</div></div></article>`}).join(''):'<div class="empty-state">所选时间内没有检测到 Listing 内容变化。</div>'
}
function rankPoint(point){
  const labels={unranked:'未进入监控页数',not_observed:'该次未显示类目排名',pending:'待积累',no_data:'无有效采样'};
  const value=point.status==='ranked'?`第${point.value}名`:(labels[point.status]||point.status);
  return `<strong class="point-${escapeHtml(point.status)}">${escapeHtml(value)}</strong>${point.observed_at?`<small>${escapeHtml(displayTime(point.observed_at))}</small>`:''}`
}
function renderHistoryBackground(){
  const background=state.asinHistory.background;
  const rows=[
    ['价格',money(background.price.start),money(background.price.end),`${money(background.price.min)} – ${money(background.price.max)}`],
    ['大类目 BSR',background.main_bsr.start??'—',background.main_bsr.end??'—',`最好 ${background.main_bsr.best??'—'} / 最低 ${background.main_bsr.worst??'—'}`],
    ['评价数',background.rating_count.start??'—',background.rating_count.end??'—',background.rating_count.change===null?'—':`净变化 ${background.rating_count.change>=0?'+':''}${background.rating_count.change}`],
  ];
  $('#historyBackground').innerHTML=rows.map(row=>`<div><span>${escapeHtml(row[0])}</span><strong>${escapeHtml(row[1])} → ${escapeHtml(row[2])}</strong><small>${escapeHtml(row[3])}</small></div>`).join('')
}
function renderHistoryProductTable(){
  const table=$('#historyProductTable'),headers=['采集时间','价格','优惠券','促销','企业价','库存状态','跟卖数','购物车卖家','发货方','页面状态'],status={active:'正常',dog:'页面变狗',removed:'商品下架'};
  table.querySelector('thead').innerHTML='<tr>'+headers.map(x=>`<th>${x}</th>`).join('')+'</tr>';
  const rows=state.asinHistory.intraday.product;
  table.querySelector('tbody').innerHTML=rows.length?rows.map(row=>`<tr><td>${escapeHtml(displayTime(row.collected_at))}</td><td>${escapeHtml(money(row.price))}</td><td>${escapeHtml(row.coupon||'—')}</td><td>${escapeHtml(row.deal||'—')}</td><td>${escapeHtml(row.business_price||'—')}</td><td>${escapeHtml(row.availability||'—')}</td><td>${row.offer_count??'—'}</td><td>${escapeHtml(row.featured_seller||'—')}</td><td>${escapeHtml(row.ships_from||'—')}</td><td>${escapeHtml(status[row.listing_status]||row.listing_status||'—')}</td></tr>`).join(''):'<tr><td colspan="10" class="empty-cell">所选时间内没有有效商品快照。</td></tr>'
}
function renderHistoryAdTable(){
  const table=$('#historyAdTable'),headers=['采集时间','关键词','采集结果','广告位'],labels={observed:'观察到广告',not_observed:'未观察到广告',collection_failed:'采集失败'};
  table.querySelector('thead').innerHTML='<tr>'+headers.map(x=>`<th>${x}</th>`).join('')+'</tr>';
  const rows=state.asinHistory.intraday.ads;
  table.querySelector('tbody').innerHTML=rows.length?rows.map(row=>`<tr><td>${escapeHtml(displayTime(row.collected_at))}</td><td>${escapeHtml(row.keyword)}</td><td><span class="observation-${escapeHtml(row.status)}">${escapeHtml(labels[row.status]||row.status)}</span></td><td>${row.ad_rank===null?'—':'第'+row.ad_rank+'位'}</td></tr>`).join(''):'<tr><td colspan="4" class="empty-cell">所选时间内没有关键词采集记录。</td></tr>'
}
function renderAsinHistory(){
  const data=state.asinHistory,current=data.current,summary=data.summary,status={active:'页面正常',dog:'页面变狗',removed:'商品下架'};
  $('#historyEmpty').hidden=true;$('#historyResults').hidden=false;$('#historyIdentity').textContent=data.identity;
  $('#historyCoverage').textContent=`实际覆盖 ${displayTime(data.range.first_sample)} 至 ${displayTime(data.range.last_sample)}，共 ${data.range.sample_count} 次有效商品快照`;
  $('#historyCurrentStatus').textContent=status[current.listing_status]||current.listing_status||'暂无快照';
  const cards=[['重要动作节点',summary.important_nodes],['价格与促销',summary.price_promo],['Listing 内容',summary.listing],['销售状态',summary.operations],['Amazon 标识',summary.platform]];
  $('#historyCards').innerHTML=cards.map(row=>`<div><span>${escapeHtml(row[0])}</span><strong>${row[1]}</strong></div>`).join('');
  renderHistoryActions();renderHistoryListing();renderHistoryBackground();renderHistoryProductTable();renderHistoryAdTable();updateHistoryLinks()
}
async function loadAsinHistory(){
  const project=$('#historyProject').value,asin=$('#historyAsin').value,days=$('#historyDays').value||'30',button=$('#loadAsinHistory');
  if(!project||!asin){toast('请先选择产品项目和竞品 ASIN',true);return}
  button.disabled=true;button.textContent='正在整理…';
  try{const d=await api(`/api/asin-history?project_id=${encodeURIComponent(project)}&asin=${encodeURIComponent(asin)}&days=${encodeURIComponent(days)}`);state.asinHistory=d.history;state.historyLoadedKey=[project,asin,days].join('|');renderAsinHistory()}catch(e){toast(e.message,true)}finally{button.disabled=false;button.textContent='查看档案'}
}
function scheduleRunRefresh(hasRunning){if(runPollTimer)clearTimeout(runPollTimer);runPollTimer=null;const delay=hasRunning||Date.now()<runPollGraceUntil?3000:15000;runPollTimer=setTimeout(()=>loadRuns().catch(e=>{toast(e.message,true);scheduleRunRefresh(false)}),delay)}
function beginRunPolling(){runPollGraceUntil=Date.now()+15000;scheduleRunRefresh(true)}
async function loadRuns(){const d=await api('/api/runs'),t=$('#runsTable'),wasRunning=state.runIsRunning,taskLabels={retry:'失败项重试',auto_retry:'自动失败项重试'};const headers=['项目','任务','开始时间','结束时间','状态','成功','失败','操作'];t.querySelector('thead').innerHTML='<tr>'+headers.map(x=>`<th>${x}</th>`).join('')+'</tr>';t.querySelector('tbody').innerHTML=d.rows.map(r=>`<tr><td>${escapeHtml(r.project_id)}</td><td>${escapeHtml(taskLabels[r.task_type]||r.task_type)}</td><td>${escapeHtml(r.started_at)}</td><td>${escapeHtml(r.finished_at||'—')}</td><td class="status-${escapeHtml(r.status)}">${escapeHtml(r.status)}</td><td>${r.success_count}</td><td>${r.failed_count}</td><td>${['partial','failed'].includes(r.status)?`<button class="retry-run" data-run-id="${escapeHtml(r.run_id)}">重试失败项</button>`:'—'}</td></tr>`).join('');t.querySelectorAll('.retry-run').forEach(b=>b.onclick=()=>retryRun(b));const latest=d.rows[0];$('#latestStatus').textContent=latest?latest.status:'暂无';state.runIsRunning=d.rows.some(r=>r.status==='running');if(wasRunning&&!state.runIsRunning){$('#runMessage').textContent='采集任务已结束，状态已自动更新';loadReports().catch(e=>toast(e.message,true))}scheduleRunRefresh(state.runIsRunning)}
async function retryRun(button){if(!confirm('只重新采集这个批次中的失败项？已成功的项目不会重复采集。'))return;button.disabled=true;try{const d=await api('/api/runs/'+encodeURIComponent(button.dataset.runId)+'/retry',{method:'POST'});$('#runMessage').textContent=d.message+'，PID '+d.pid;toast('失败项重试已启动');beginRunPolling()}catch(e){toast(e.message,true);button.disabled=false}}
async function loadReports(){
  const d=await api('/api/reports');
  const summaries=`<div class="report-item summary-report-item"><strong class="summary-report-label">汇总简报</strong><span class="report-links summary-report-links"><a href="/reports/summary/daily"><span>昨日日报 Excel</span><small>${escapeHtml(d.daily.date)}</small></a><a href="/reports/summary/weekly"><span>上周周报 Excel</span><small>${escapeHtml(d.weekly.start_date)} 至 ${escapeHtml(d.weekly.end_date)}</small></a></span></div>`;
  const projects=d.rows.map(r=>`<div class="report-item"><strong>${escapeHtml(r.project_name)}</strong><span class="report-links">${r.main_exists?`<a href="/reports/${encodeURIComponent(r.project_id)}/main" title="${escapeHtml(r.main_filename)}">主报告</a>`:'<em>暂无报告</em>'}</span></div>`).join('');
  $('#reportList').innerHTML=summaries+projects
}
async function startRun(){const b=$('#runButton');b.disabled=true;try{const d=await api('/api/run',{method:'POST',body:JSON.stringify({project:$('#runProject').value,task:$('#runTask').value})});$('#runMessage').textContent=d.message+'，PID '+d.pid;toast('采集任务已启动');beginRunPolling()}catch(e){toast(e.message,true)}finally{b.disabled=false}}
async function loadSchedule(){const d=await api('/api/schedule'),s=d.schedule;$('#scheduleEnabled').checked=!!s.enabled;$('#scheduleTimes').value=(s.times||[]).join(', ');$('#wakeEnabled').checked=s.wake_enabled!==false;$('#wakeLeadMinutes').value=s.wake_lead_minutes||5;$('#scheduleTask').value=s.task||'all';$('#scheduleProject').value=s.project||'all';await scheduleStatus()}
function currentSchedule(){return {enabled:$('#scheduleEnabled').checked,times:$('#scheduleTimes').value.split(',').map(x=>x.trim()).filter(Boolean),task:$('#scheduleTask').value,project:$('#scheduleProject').value,wake_enabled:$('#wakeEnabled').checked,wake_lead_minutes:$('#wakeLeadMinutes').value}}
async function saveSchedule(showToast=true){try{await api('/api/schedule',{method:'PUT',body:JSON.stringify({schedule:currentSchedule()})});if(showToast)toast('定时设置已保存');return true}catch(e){toast(e.message,true);$('#scheduleOutput').textContent=e.message;return false}}
async function installSchedule(){const button=$('#installSchedule');button.disabled=true;$('#scheduleEnabled').checked=true;try{if(await saveSchedule(false))await scheduleAction('install')}finally{button.disabled=false}}
async function scheduleAction(action){if(action==='uninstall'&&!confirm('确认卸载系统定时任务？历史数据不会删除。'))return;try{const d=await api('/api/schedule/'+action,{method:'POST'});$('#scheduleOutput').textContent=d.output;toast(action==='install'?'定时任务已安装':'定时任务已卸载');await scheduleStatus()}catch(e){$('#scheduleOutput').textContent=e.message;toast(e.message,true)}}
async function scheduleStatus(){try{const d=await api('/api/schedule/status');$('#scheduleOutput').textContent=d.output;const installed=d.output.includes('系统状态: 已安装');$('#scheduleStatus').textContent=installed?'已安装':'未安装';$('#scheduleStatus').className='badge '+(installed?'':'muted')}catch(e){$('#scheduleOutput').textContent=e.message}}
function currentNotification(){return {enabled:$('#notificationEnabled').checked,time:$('#notificationTime').value,send_when_no_changes:$('#notifyNoChanges').checked,max_items:$('#notificationMaxItems').value}}
async function loadNotification(){const d=await api('/api/notification'),c=d.config;$('#notificationEnabled').checked=!!c.enabled;$('#notificationTime').value=c.time||'09:00';$('#notifyNoChanges').checked=c.send_when_no_changes!==false;$('#notificationMaxItems').value=c.max_items||15;$('#serverchanSendkey').placeholder=d.has_sendkey?'已安全保存；留空表示不修改':'请输入 SCT 开头的 SendKey';const t=$('#notificationLogs'),headers=['简报','汇总日期','发送时间','结果','内容数','说明','操作'];t.querySelector('thead').innerHTML='<tr>'+headers.map(x=>`<th>${x}</th>`).join('')+'</tr>';t.querySelector('tbody').innerHTML=d.logs.map(r=>{const type=r.report_type==='collection_alert'?'采集异常':r.report_type==='weekly'?'周报':'日报',download=r.download_url?`<a class="table-link" href="${escapeHtml(r.download_url)}">下载 Excel</a>`:'',retry=r.retryable?`<button class="retry-notification" data-log-id="${r.id}">重新发送</button>`:'',retryState=!r.success&&!r.retryable&&r.report_type!=='collection_alert'?'<span class="muted-text">已补发</span>':'';return `<tr><td>${escapeHtml(type)}</td><td>${escapeHtml(r.report_date)}</td><td>${escapeHtml(r.sent_at)}</td><td class="status-${r.success?'success':'failed'}">${r.success?'成功':'失败'}</td><td>${r.item_count}</td><td>${escapeHtml(r.response_message||'')}</td><td><span class="table-actions"><button class="view-notification" data-log-id="${r.id}">查看简报</button>${download}${retry}${retryState}</span></td></tr>`}).join('');t.querySelectorAll('.view-notification').forEach(b=>b.onclick=()=>viewNotification(b));t.querySelectorAll('.retry-notification').forEach(b=>b.onclick=()=>retryNotification(b));await notificationScheduleStatus()}
async function viewNotification(button){button.disabled=true;try{const d=await api('/api/notification/logs/'+encodeURIComponent(button.dataset.logId)),rebuilt=d.row.body_source==='rebuilt';$('#notificationPreviewTitle').textContent=(d.row.title||'简报内容')+(rebuilt?'（根据现存数据重建）':'');$('#notificationPreviewBody').textContent=(rebuilt?'说明：这条记录生成于正文留存功能上线前，以下内容根据当前仍保留的数据重新生成，可能与当时实际发送版本略有差异。\n\n':'')+(d.row.body||'该记录没有可显示的正文。');$('#notificationPreview').hidden=false;$('#notificationPreview').scrollIntoView({behavior:'smooth',block:'nearest'})}catch(e){toast(e.message,true)}finally{button.disabled=false}}
async function retryNotification(button){if(!confirm('重新发送这条失败的简报？'))return;button.disabled=true;$('#notificationOutput').textContent='正在重新发送；如果遇到网络异常，系统会在30秒、2分钟和5分钟后自动重试，请不要重复点击。';try{const d=await api('/api/notification/logs/'+encodeURIComponent(button.dataset.logId)+'/retry',{method:'POST'});toast(d.report_type==='weekly'?'周报已重新发送':'日报已重新发送');$('#notificationOutput').textContent='失败简报已重新发送，共 '+(d.item_count??0)+' 项内容'}catch(e){toast(e.message,true);$('#notificationOutput').textContent=e.message}finally{await loadNotification()}}
async function saveNotification(showToast=true){try{const d=await api('/api/notification',{method:'PUT',body:JSON.stringify({config:currentNotification(),sendkey:$('#serverchanSendkey').value})});$('#serverchanSendkey').value='';$('#serverchanSendkey').placeholder=d.has_sendkey?'已安全保存；留空表示不修改':'请输入 SCT 开头的 SendKey';if(showToast)toast('微信通知设置已保存');return true}catch(e){toast(e.message,true);$('#notificationOutput').textContent=e.message;return false}}
async function testNotification(){const b=$('#testNotification');b.disabled=true;try{if(!await saveNotification(false))return;const d=await api('/api/notification/test',{method:'POST'});$('#notificationOutput').textContent='测试消息发送成功：'+d.message;toast('请检查手机微信')}catch(e){$('#notificationOutput').textContent=e.message;toast(e.message,true)}finally{b.disabled=false;await loadNotification()}}
async function sendYesterday(){const b=$('#sendYesterday');if(!confirm('立即发送一次前一日竞品变化摘要？'))return;b.disabled=true;try{if(!await saveNotification(false))return;$('#notificationOutput').textContent='正在发送昨日摘要；如果遇到网络异常，系统会自动退避重试，请不要重复点击。';const d=await api('/api/notification/send-yesterday',{method:'POST'});$('#notificationOutput').textContent='昨日摘要已发送，共 '+d.item_count+' 项变化';toast('昨日摘要已发送')}catch(e){$('#notificationOutput').textContent=e.message;toast(e.message,true)}finally{b.disabled=false;await loadNotification()}}
async function sendWeekly(){const b=$('#sendWeekly');if(!confirm('立即发送一次上一个自然周的重点复盘、评价、排名趋势与广告投放观察周报？'))return;b.disabled=true;try{if(!await saveNotification(false))return;$('#notificationOutput').textContent='正在发送上周周报；如果遇到网络异常，系统会自动退避重试，请不要重复点击。';const d=await api('/api/notification/send-weekly',{method:'POST'});$('#notificationOutput').textContent='上周周报已发送，共 '+d.item_count+' 项内容';toast('上周周报已发送')}catch(e){$('#notificationOutput').textContent=e.message;toast(e.message,true)}finally{b.disabled=false;await loadNotification()}}
async function notificationScheduleAction(action){if(action==='uninstall'&&!confirm('确认卸载微信日报定时任务？SendKey和发送记录会保留。'))return;try{if(action==='install'){ $('#notificationEnabled').checked=true;if(!await saveNotification(false))return }const d=await api('/api/notification/schedule/'+action,{method:'POST'});$('#notificationOutput').textContent=d.output;toast(action==='install'?'微信定时发送已安装':'微信定时发送已卸载');await notificationScheduleStatus()}catch(e){$('#notificationOutput').textContent=e.message;toast(e.message,true)}}
async function notificationScheduleStatus(){try{const d=await api('/api/notification/schedule/status');const installed=d.output.includes('系统状态: 已安装');$('#notificationStatus').textContent=installed?'已安装':'未安装';$('#notificationStatus').className='badge '+(installed?'':'muted');if(!$('#notificationOutput').textContent)$('#notificationOutput').textContent=d.output}catch(e){$('#notificationOutput').textContent=e.message}}
function splitRuleWords(value){return value.split(/[\n,;，；]+/).map(x=>x.trim()).filter(Boolean)}
async function loadNewProducts(){const d=await api('/api/new-products');state.newProductRows=d.rows;state.newProductRules=d.rules;const rules=$('#newProductRules');rules.innerHTML=state.projects.filter(x=>Number(x.enabled)).map(p=>{const r=d.rules[p.project_id]||{include_keywords:[],exclude_keywords:[]};return `<div class="rule-card" data-project-id="${escapeHtml(p.project_id)}"><strong>${escapeHtml(p.project_name)}<br><small>${escapeHtml(p.project_id)}</small></strong><label>同类包含关键词<textarea class="rule-include" rows="3">${escapeHtml((r.include_keywords||[]).join('\n'))}</textarea></label><label>排除关键词<textarea class="rule-exclude" rows="3">${escapeHtml((r.exclude_keywords||[]).join('\n'))}</textarea></label></div>`}).join('');const filter=$('#newProductProjectFilter'),current=filter.value||'all';selectOptions(filter);if([...filter.options].some(x=>x.value===current))filter.value=current;renderNewProductRows()}
function renderNewProductRows(){
  const project=$('#newProductProjectFilter').value||'all',status=$('#newProductStatusFilter').value||'recommended';
  const rows=state.newProductRows.filter(r=>(project==='all'||r.project_id===project)&&(status==='all'||(status==='recommended'?r.recommended:r.relevance_status===status)));
  const t=$('#newProductTable'),labels={pending:'待确认',same:'同类竞品',not_same:'非同类产品'},dateSourceLabels={auto:'自动采集',manual:'人工填写'};
  const headers=['项目','商品','类目 / 当前排名','近7日日中位路径','趋势判断','同类判断','上架日期（参考）','人工确认'];
  t.querySelector('thead').innerHTML='<tr>'+headers.map(x=>`<th>${x}</th>`).join('')+'</tr>';
  t.querySelector('tbody').innerHTML=rows.map(r=>{
    const dateEditor=`<div class="candidate-date-editor"><input class="candidate-date" type="date" value="${escapeHtml(r.date_first_available||'')}" data-project-id="${escapeHtml(r.project_id)}" data-asin="${escapeHtml(r.asin)}"><button class="save-candidate-date">保存</button></div><small>${escapeHtml(dateSourceLabels[r.date_source]||(r.date_first_available?'自动采集':'尚未获取'))}</small>`;
    const action=`<select class="candidate-status" data-project-id="${escapeHtml(r.project_id)}" data-asin="${escapeHtml(r.asin)}"><option value="pending" ${r.relevance_status==='pending'?'selected':''}>待确认</option><option value="same" ${r.relevance_status==='same'?'selected':''}>同类竞品</option><option value="not_same" ${r.relevance_status==='not_same'?'selected':''}>非同类产品</option></select>`;
    const path=(r.daily_path||[]).map(x=>`${escapeHtml(String(x.date).slice(5))} 第${x.rank}名`).join(' → ')||'观测数据不足';
    const confidence=r.recommended?`<span class="badge radar-${escapeHtml(r.confidence)}">${r.confidence==='high'?'高信心推荐':'推荐关注'}</span>`:'<span class="badge muted">暂不推荐</span>';
    const typeReason=`${labels[r.relevance_status]||escapeHtml(r.relevance_status)}${r.relevance_reason?'<small>'+escapeHtml(r.relevance_reason)+'</small>':''}`;
    return `<tr><td>${escapeHtml(r.project_name)}</td><td><a href="${escapeHtml(r.detail_url)}" target="_blank" rel="noopener">${escapeHtml(r.product)}</a></td><td>${escapeHtml(r.category_name)}：${r.current_rank??'—'}</td><td class="radar-path">${path}</td><td>${confidence}<small class="radar-reason">${escapeHtml(r.reason||'')}</small></td><td>${typeReason}</td><td>${dateEditor}<small>上架天数：${r.age_days??'未知'}</small></td><td>${action}</td></tr>`
  }).join('');
  t.querySelectorAll('.candidate-status').forEach(s=>s.onchange=()=>updateCandidateStatus(s));
  t.querySelectorAll('.save-candidate-date').forEach(b=>b.onclick=()=>updateCandidateDate(b))
}
async function saveNewProductRules(){const rules={};$$('#newProductRules .rule-card').forEach(card=>{rules[card.dataset.projectId]={include_keywords:splitRuleWords(card.querySelector('.rule-include').value),exclude_keywords:splitRuleWords(card.querySelector('.rule-exclude').value)}});try{await api('/api/new-products/rules',{method:'PUT',body:JSON.stringify({rules})});toast('同类竞品筛选规则已保存');await loadNewProducts()}catch(e){toast(e.message,true)}}
async function updateCandidateStatus(select){select.disabled=true;try{await api('/api/new-products/candidate',{method:'PUT',body:JSON.stringify({project_id:select.dataset.projectId,asin:select.dataset.asin,status:select.value})});toast('人工确认已保存');await loadNewProducts()}catch(e){toast(e.message,true);await loadNewProducts()}}
async function updateCandidateDate(button){const input=button.parentElement.querySelector('.candidate-date');button.disabled=true;try{await api('/api/new-products/date',{method:'PUT',body:JSON.stringify({project_id:input.dataset.projectId,asin:input.dataset.asin,date_first_available:input.value})});toast(input.value?'上架日期已保存':'上架日期已清除');await loadNewProducts();await loadReports()}catch(e){toast(e.message,true);button.disabled=false}}
async function activateTab(button){const current=$('#tabs button.active')?.dataset.tab;if(current==='competitors'&&button.dataset.tab!=='competitors'&&state.dirtyConfigs.has('competitors')){if(!confirm('竞品 ASIN 有未保存的修改。是否先保存？\n\n点击“确定”保存后切换；点击“取消”留在当前页面。'))return;if(!await saveTable('competitors',state.headers.competitors,$('#competitors')))return}$$('#tabs button').forEach(x=>x.classList.remove('active'));$$('.panel').forEach(x=>x.classList.remove('active'));button.classList.add('active');$('#'+button.dataset.tab).classList.add('active');if(button.dataset.tab==='asinHistory'){const key=[$('#historyProject').value,$('#historyAsin').value,$('#historyDays').value].join('|');if($('#historyAsin').value&&state.historyLoadedKey!==key)await loadAsinHistory()}}
function bind(){ $$('#tabs button').forEach(b=>b.onclick=()=>activateTab(b));window.addEventListener('beforeunload',event=>{if(!state.dirtyConfigs.has('competitors'))return;event.preventDefault();event.returnValue=''});$('#runButton').onclick=startRun;$('#refreshRuns').onclick=loadRuns;$('#refreshReports').onclick=loadReports;$('#loadAsinHistory').onclick=loadAsinHistory;$('#historyProject').onchange=()=>{populateHistoryAsins();invalidateHistoryView()};$('#historyAsin').onchange=invalidateHistoryView;$('#historyDays').onchange=invalidateHistoryView;$('#historyCategory').onchange=renderHistoryActions;$('#saveSchedule').onclick=()=>saveSchedule();$('#installSchedule').onclick=installSchedule;$('#uninstallSchedule').onclick=()=>scheduleAction('uninstall');$('#saveNotification').onclick=()=>saveNotification();$('#testNotification').onclick=testNotification;$('#sendYesterday').onclick=sendYesterday;$('#sendWeekly').onclick=sendWeekly;$('#installNotification').onclick=()=>notificationScheduleAction('install');$('#uninstallNotification').onclick=()=>notificationScheduleAction('uninstall');$('#refreshNotification').onclick=loadNotification;$('#closeNotificationPreview').onclick=()=>$('#notificationPreview').hidden=true;$('#saveNewProductRules').onclick=saveNewProductRules;$('#refreshNewProducts').onclick=loadNewProducts;$('#newProductProjectFilter').onchange=renderNewProductRows;$('#newProductStatusFilter').onchange=renderNewProductRows}
async function health(){try{await api('/api/health');$('#systemBadge').textContent='服务正常';$('#systemBadge').style.background='rgba(255,255,255,.12)'}catch(e){$('#systemBadge').textContent='服务已断开';$('#systemBadge').style.background='#9e342d'}}
async function init(){bind();try{for(const kind of Object.keys(labels))await loadConfig(kind);await Promise.all([loadRuns(),loadReports(),loadSchedule(),loadNotification(),loadNewProducts()]);await health();setInterval(health,15000)}catch(e){toast(e.message,true);$('#systemBadge').textContent='服务已断开';$('#systemBadge').style.background='#9e342d'}}
init();
