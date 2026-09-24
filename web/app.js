// 所有资料处理均由本机服务完成，界面不加载任何远程脚本或字体。
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
let config, jobId, model, pollTimer, mode='ppt';
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const people = value => value.split(/[、,，;；\n]+/).map(s => s.trim()).filter(Boolean);
const roleFields = {
  cover:['title','metadata','meeting'], opening:['title','people','meeting'], summary:['title','people','meeting'],
  talk:['title','people','meeting'], discussion:['title','people','meeting'],
  chair:['identity','hospital','bio','photo','role','meeting'], host:['identity','hospital','bio','photo','role','meeting'],
  speaker:['identity','hospital','bio','photo','role','meeting'], guest:['identity','hospital','bio','photo','role','meeting'],
  topics:['meeting'], ending:['meeting'], unknown:['meeting']
};
const fieldNames = {title:'主标题',metadata:'日期主办',meeting:'会议名称',people:'人员名单',identity:'专家姓名',hospital:'独立单位框',bio:'专家简介',photo:'专家照片',role:'角色标签'};

function notice(text='') { $('#notice').textContent=text; $('#notice').hidden=!text; }
function showPage(id) { const step=id.includes('result')?'result':id.includes('review')?'review':'upload'; $$('.page').forEach(p=>p.hidden=p.id!==id); $$('.step').forEach(b=>b.classList.toggle('active',b.dataset.step===step)); window.scrollTo(0,0); }
function busy(show,message='') { $('#busy').hidden=!show; $('#busy-message').textContent=message; $('#busy-title').textContent=mode==='poster'?(model?'正在生成会议海报':'正在识别海报资料'):(model?'正在生成会议PPT':'正在分析会议资料'); }
async function request(url,options={}) {
  if (options.method==='POST') options.headers={...options.headers,'X-AutoPPT-Token':config.token};
  const response=await fetch(url,options); const value=await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败'); return value;
}
function jsonPost(url,data) { return request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); }
async function begin(url,body,isJSON=false,requestedMode='ppt') {
  notice(); mode=requestedMode; model=null; busy(true,mode==='poster'?'正在进行本地OCR并匹配专家头像':'正在读取原生文字、照片和模板结构');
  try { const result=isJSON?await jsonPost(url,body):await request(url,{method:'POST',body}); jobId=result.id; poll(); }
  catch(error) { busy(false); notice(error.message); }
}
async function poll() {
  clearTimeout(pollTimer);
  try {
    const job=await request(`/api/jobs/${jobId}`); $('#busy-message').textContent=job.message;
    if (['analyzing','generating'].includes(job.status)) { pollTimer=setTimeout(poll,650); return; }
    busy(false);
    if (job.status==='failed') { notice(job.message); return; }
    if (job.status==='complete') { model=job.model; if(job.kind==='poster'){renderPosterResult(job);showPage('poster-result');}else{renderResult(job);showPage('result');} return; }
    if (job.generation_error) { notice(job.generation_error); return; }
    model=job.model; if(job.kind==='poster'){renderPosterReview();showPage('poster-review');}else{renderReview();showPage('review');}
  } catch(error) { busy(false); notice(error.message); }
}
function stats(target,values) { $(target).innerHTML=values.map(([value,label])=>`<div class="stat"><b>${escapeHTML(value)}</b><span>${escapeHTML(label)}</span></div>`).join(''); }
function renderReview() {
  stats('#stats',[[model.experts.length,'份专家简介'],[model.agenda.events.length,'个日程环节'],[model.template.slides.length,'页原始模板'],[model.metrics.analysis_seconds+'s','本次识别用时']]);
  $('#issues-summary').textContent=`识别提示 · ${model.issues.length}项`;
  $('#issues').innerHTML=model.issues.map(i=>`<div class="issue ${escapeHTML(i.level)}">${escapeHTML(i.message)}</div>`).join('') || '未发现资料缺失';
  $('#issues-box').open=model.issues.some(i=>i.level==='error');
  ['title','date','venue','organizer'].forEach(key=>{const input=$('#meeting-'+key);input.value=model.agenda[key];input.oninput=()=>model.agenda[key]=input.value;});
  renderAgenda(); renderExperts(); renderTemplate();
  $('#option-repeat').checked=model.options.repeat_chairs; $('#option-topics').checked=model.options.include_topics; $('#option-draft').checked=model.options.draft;
}
function renderAgenda() {
  const kinds={opening:'主席致辞',talk:'专题讲题',discussion:'讨论环节',summary:'会议总结'};
  $('#agenda-body').innerHTML=model.agenda.events.map((e,i)=>`<tr data-event="${i}"><td><input aria-label="时间" data-field="time" value="${escapeHTML(e.time)}"></td><td><select aria-label="环节类型" data-field="kind">${Object.entries(kinds).map(([k,v])=>`<option value="${k}" ${e.kind===k?'selected':''}>${v}</option>`).join('')}</select></td><td><textarea aria-label="环节内容" rows="2" data-field="title">${escapeHTML(e.title)}</textarea></td><td><textarea aria-label="环节嘉宾" rows="2" data-field="people">${escapeHTML(e.people.join('、'))}</textarea></td><td><input aria-label="主持人" data-field="hosts" value="${escapeHTML(e.hosts.join('、'))}"></td><td><button aria-label="删除环节" class="delete" data-remove="${i}">×</button></td></tr>`).join('');
  $$('#agenda-body [data-field]').forEach(input=>input.oninput=()=>{const e=model.agenda.events[+input.closest('tr').dataset.event];e[input.dataset.field]=['people','hosts'].includes(input.dataset.field)?people(input.value):input.value;});
  $$('#agenda-body [data-remove]').forEach(button=>button.onclick=()=>{model.agenda.events.splice(+button.dataset.remove,1);renderAgenda();});
}
function renderExperts() {
  $('#experts-list').innerHTML=model.experts.map((e,i)=>{
    const detail=model.agenda.people[e.name]||{};const current=e.photos.find(p=>p.image===e.photo);
    return `<div class="panel expert-card" data-expert="${i}"><div>${current?`<img class="expert-photo" src="${current.thumbnail}" alt="${escapeHTML(e.name)}资料照片">`:'<div class="expert-photo"></div>'}<div class="expert-source">${escapeHTML(e.source)}</div>${e.photos.length>1?`<select aria-label="选择照片" data-expert-field="photo">${e.photos.map((p,j)=>`<option value="${escapeHTML(p.image)}" ${p.image===e.photo?'selected':''}>候选照片${j+1}</option>`).join('')}</select>`:''}${!e.photo_confirmed?'<label class="hint"><input type="checkbox" data-confirm-photo>已确认当前照片</label>':''}</div><div class="expert-fields"><label>姓名<input data-expert-field="name" value="${escapeHTML(e.name)}"></label><label>会议展示单位<input data-person-field="hospital" value="${escapeHTML(detail.hospital||e.hospital)}"></label><label>会议展示称呼<input data-person-field="display_title" value="${escapeHTML(detail.display_title||'')}"></label><label class="bio">简介原文 · 生成时整理为最多8条<textarea data-expert-field="bio">${escapeHTML(e.bio.join('\n'))}</textarea></label></div></div>`;
  }).join('');
  $$('#experts-list [data-expert-field]').forEach(input=>input.onchange=()=>{
    const e=model.experts[+input.closest('[data-expert]').dataset.expert];const key=input.dataset.expertField;
    if(key==='name'){const old=e.name;model.agenda.events.forEach(event=>['people','hosts'].forEach(k=>event[k]=event[k].map(n=>n===old?input.value:n)));model.agenda.people[input.value]=model.agenda.people[old]||{};delete model.agenda.people[old];}
    e[key]=key==='bio'?input.value.split('\n').filter(s=>s.trim()):input.value;
    if(key==='photo'){const p=e.photos.find(p=>p.image===e.photo);input.closest('.expert-card').querySelector('img').src=p.thumbnail;}
  });
  $$('#experts-list [data-person-field]').forEach(input=>input.oninput=()=>{const e=model.experts[+input.closest('[data-expert]').dataset.expert];model.agenda.people[e.name]??={hospital:e.hospital,display_title:''};model.agenda.people[e.name][input.dataset.personField]=input.value;});
  $$('#experts-list [data-confirm-photo]').forEach(input=>input.onchange=()=>{model.experts[+input.closest('[data-expert]').dataset.expert].photo_confirmed=input.checked;});
}
function structure(slide,width,height) {
  const items=slide.shapes.filter(s=>s.text||s.thumbnail).map(s=>{
    const [x,y,w,h]=s.bbox;const pos=`left:${x/width*100}%;top:${y/height*100}%;width:${w/width*100}%;height:${h/height*100}%`;
    return `<div class="object ${s.thumbnail?'picture':''}" style="${pos}" title="${escapeHTML(s.name+' · '+s.id+' · '+s.text)}">${s.thumbnail?`<img src="${s.thumbnail}" alt="模板图片">`:escapeHTML(s.text)}</div>`;
  }).join('');
  return `<div class="structure" style="aspect-ratio:${width}/${height}">${items}</div>`;
}
function renderTemplate() {
  const template=model.template;
  $('#template-list').innerHTML=template.slides.map((s,i)=>{
    const mapping=(roleFields[s.role]||[]).map(key=>{
      const multiple=['meeting','metadata'].includes(key);const selected=multiple?(s.fields[key]||[]):[s.fields[key]];
      const shapes=s.shapes.filter(shape=>key==='photo'?shape.kind==='pic':Boolean(shape.text));
      return `<label>${fieldNames[key]}<select data-mapping="${key}" ${multiple?'multiple':''}><option value="">不填充</option>${shapes.map(shape=>`<option value="${shape.id}" ${selected.includes(shape.id)?'selected':''}>${escapeHTML(shape.id+' · '+(shape.text||shape.name).slice(0,42))}</option>`).join('')}</select></label>`;
    }).join('');
    return `<div class="template-card" data-slide="${i}">${structure(s,template.width,template.height)}<div class="card-head"><span class="number">第${s.number}页</span><select aria-label="页面用途" data-role>${Object.entries(config.roles).map(([k,v])=>`<option value="${k}" ${s.role===k?'selected':''}>${v}</option>`).join('')}</select></div><details class="mapping"><summary>查看与调整填充区域</summary>${mapping}</details></div>`;
  }).join('');
  $$('#template-list [data-role]').forEach(select=>select.onchange=()=>{model.template.slides[+select.closest('[data-slide]').dataset.slide].role=select.value;renderTemplate();});
  $$('#template-list [data-mapping]').forEach(select=>select.onchange=()=>{const slide=model.template.slides[+select.closest('[data-slide]').dataset.slide];slide.fields[select.dataset.mapping]=select.multiple?[...select.selectedOptions].map(o=>o.value).filter(Boolean):select.value;});
}
function renderResult(job) {
  const report=job.report;
  $('#result-description').textContent=report.draft?'当前为待核对版本。请补充缺失资料后再用于正式会议。':'已完成模板填充及结构、人员内容校验，可下载并检查实际播放效果。';
  stats('#result-stats',[[report.pages.length,'页生成内容'],[report.metrics.generation_seconds+'s','生成与结构校验用时'],[report.metrics.ai_calls,'次AI调用']]);
  $('#output-name').textContent=report.file;$('#download').href=`/api/jobs/${jobId}/download`;$('#report-download').href=`/api/jobs/${jobId}/report`;
  $('#result-issues').innerHTML=report.issues.map(i=>`<div class="issue ${escapeHTML(i.level)}">${escapeHTML(i.message)}</div>`).join('')||'未发现生成过程中的异常。';
  const deck=job.output_slides;
  $('#output-preview').innerHTML=deck.slides.map((s,i)=>`<div class="template-card">${structure(s,deck.width,deck.height)}<div class="card-head"><span class="number">第${i+1}页</span><span>${escapeHTML(config.roles[report.pages[i].role])}${report.pages[i].expert?' · '+escapeHTML(report.pages[i].expert):''}</span></div></div>`).join('');
}

function renderPosterAgenda() {
  const kinds={opening:'主席致辞',talk:'专题讲题',discussion:'互动讨论',summary:'会议总结'};
  $('#poster-agenda-body').innerHTML=model.agenda.events.map((event,index)=>`<tr data-poster-event="${index}"><td><input aria-label="时间" data-poster-field="time" value="${escapeHTML(event.time)}"></td><td><select aria-label="环节类型" data-poster-field="kind">${Object.entries(kinds).map(([key,label])=>`<option value="${key}" ${event.kind===key?'selected':''}>${label}</option>`).join('')}</select></td><td><textarea aria-label="主题" rows="2" data-poster-field="title">${escapeHTML(event.title)}</textarea></td><td><textarea aria-label="讲者或嘉宾" rows="2" data-poster-field="people">${escapeHTML(event.people.join('、'))}</textarea></td><td><input aria-label="主持" data-poster-field="hosts" value="${escapeHTML(event.hosts.join('、'))}"></td><td><button aria-label="删除环节" class="delete" data-poster-remove="${index}">×</button></td></tr>`).join('');
  $$('#poster-agenda-body [data-poster-field]').forEach(input=>input.oninput=()=>{const event=model.agenda.events[+input.closest('tr').dataset.posterEvent];event[input.dataset.posterField]=['people','hosts'].includes(input.dataset.posterField)?people(input.value):input.value;});
  $$('#poster-agenda-body [data-poster-remove]').forEach(button=>button.onclick=()=>{model.agenda.events.splice(+button.dataset.posterRemove,1);renderPosterAgenda();});
}

function renderPosterExperts() {
  const roles={chair:'会议主席',host:'会议主持',speaker:'会议讲者',guest:'讨论嘉宾'};
  $('#poster-experts-list').innerHTML=model.experts.map((expert,index)=>`<div class="poster-expert" data-poster-expert="${index}"><img src="${expert.thumbnail}" alt="${escapeHTML(expert.name)}头像"><div><label>姓名<input data-poster-expert-field="name" value="${escapeHTML(expert.name)}"></label><label>单位<input data-poster-expert-field="hospital" value="${escapeHTML(expert.hospital)}"></label><label>称呼<input data-poster-expert-field="display_title" value="${escapeHTML(expert.display_title)}"></label><label>海报分组<select data-poster-expert-field="role">${Object.entries(roles).map(([key,label])=>`<option value="${key}" ${expert.role===key?'selected':''}>${label}</option>`).join('')}</select></label></div></div>`).join('');
  $$('#poster-experts-list [data-poster-expert-field]').forEach(input=>input.onchange=()=>{
    const expert=model.experts[+input.closest('[data-poster-expert]').dataset.posterExpert];const key=input.dataset.posterExpertField;
    if(key==='name'){const old=expert.name;model.agenda.chairs=model.agenda.chairs.map(name=>name===old?input.value:name);model.agenda.events.forEach(event=>['people','hosts'].forEach(field=>event[field]=event[field].map(name=>name===old?input.value:name)));}
    expert[key]=input.value;
  });
}

function renderPosterReview() {
  stats('#poster-stats',[[model.experts.length,'位专家头像'],[model.agenda.events.length,'个日程环节'],[model.agenda.ocr_items,'处OCR文字'],[model.metrics.analysis_seconds+'s','本次识别用时']]);
  $('#poster-issues-summary').textContent=`识别提示 · ${model.issues.length}项`;
  $('#poster-issues').innerHTML=model.issues.map(issue=>`<div class="issue ${escapeHTML(issue.level)}">${escapeHTML(issue.message)}</div>`).join('')||'日程和头像匹配完整';
  $('#poster-issues-box').open=model.issues.some(issue=>issue.level==='error');
  const bindings={title:'poster-title',date:'poster-date',meeting_time:'poster-time',meeting_code:'poster-code',organizer:'poster-organizer'};
  Object.entries(bindings).forEach(([key,id])=>{const input=$('#'+id);input.value=model.agenda[key]||'';input.oninput=()=>model.agenda[key]=input.value;});
  $('#poster-chairs').value=model.agenda.chairs.join('、');$('#poster-chairs').oninput=()=>model.agenda.chairs=people($('#poster-chairs').value);
  renderPosterAgenda();renderPosterExperts();
}

function renderPosterResult(job) {
  const report=job.report;
  stats('#poster-result-stats',[[report.experts,'位专家'],[report.events,'个日程环节'],[`${report.width}×${report.height}`,'海报像素'],[report.metrics.generation_seconds+'s','生成用时']]);
  $('#poster-preview').src=job.preview;$('#poster-download').href=`/api/jobs/${jobId}/download`;
}
async function init() {
  try {config=await request('/api/config');}catch(error){notice(error.message);return;}
  $('#samples').innerHTML=config.samples.length?'<span>使用项目中的现有资料体验</span>'+config.samples.map(s=>`<button data-sample="${s}">${s==='input'?'9月7日临床营养会议':'8月13日胸部肿瘤会议'}</button>`).join(''):'';
  $$('[data-sample]').forEach(b=>b.onclick=()=>begin('/api/sample',{name:b.dataset.sample},true,'ppt'));
  $$('[data-tool]').forEach(button=>button.onclick=()=>{$$('[data-tool]').forEach(item=>item.classList.toggle('selected',item===button));$('#ppt-tool').hidden=button.dataset.tool!=='ppt';$('#poster-tool').hidden=button.dataset.tool!=='poster';mode=button.dataset.tool;});
  $$('input[type=file]').forEach(input=>input.onchange=()=>{input.parentElement.querySelector('.file-state').textContent=input.files.length?`${input.files.length}个文件 · ${input.files[0].name}`:'点击选择文件';});
  $('#upload-form').onsubmit=event=>{event.preventDefault();begin('/api/analyze',new FormData(event.target),false,'ppt');};
  $('#poster-upload-form').onsubmit=event=>{event.preventDefault();begin('/api/poster/analyze',new FormData(event.target),false,'poster');};
  $('#back-upload').onclick=()=>showPage('upload');$('#back-review').onclick=()=>{renderReview();showPage('review');};
  $('#poster-back-upload').onclick=()=>showPage('upload');$('#poster-back-review').onclick=()=>{renderPosterReview();showPage('poster-review');};
  $$('.step').forEach(button=>button.onclick=()=>{if(button.dataset.step==='upload')showPage('upload');else if(button.dataset.step==='review'&&model)showPage(mode==='poster'?'poster-review':'review');});
  $$('.tabs button').forEach(button=>button.onclick=()=>{$$('.tabs button').forEach(b=>b.classList.toggle('selected',b===button));$$('.tab-panel').forEach(p=>p.hidden=p.id!==button.dataset.tab);if(button.dataset.tab==='agenda-panel')renderAgenda();});
  $('#add-event').onclick=()=>{model.agenda.events.push({time:'',kind:'talk',title:'',people:[],hosts:[]});renderAgenda();};
  $('#poster-add-event').onclick=()=>{model.agenda.events.push({time:'',kind:'talk',title:'',people:[],hosts:[]});renderPosterAgenda();};
  $('#generate').onclick=async()=>{
    notice(); model.options.repeat_chairs=$('#option-repeat').checked;model.options.include_topics=$('#option-topics').checked;model.options.draft=$('#option-draft').checked;
    busy(true,'正在复制模板页面并写入本次会议资料');
    try {await jsonPost(`/api/jobs/${jobId}/generate`,model);poll();}catch(error){busy(false);notice(error.message);}
  };
  $('#poster-generate').onclick=async()=>{
    notice();busy(true,'正在按模板比例排版专家头像和会议日程');
    try{await jsonPost(`/api/jobs/${jobId}/poster-generate`,model);poll();}catch(error){busy(false);notice(error.message);}
  };
}
init();
