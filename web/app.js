// 所有资料处理均由本机服务完成，界面不加载任何远程脚本或字体。
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
let config, jobId, model, pollTimer;
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
function showPage(id) { $$('.page').forEach(p=>p.hidden=p.id!==id); $$('.step').forEach(b=>b.classList.toggle('active',b.dataset.step===id)); window.scrollTo(0,0); }
function busy(show,message='') { $('#busy').hidden=!show; $('#busy-message').textContent=message; $('#busy-title').textContent=model?'正在生成会议PPT':'正在分析会议资料'; }
async function request(url,options={}) {
  if (options.method==='POST') options.headers={...options.headers,'X-AutoPPT-Token':config.token};
  const response=await fetch(url,options); const value=await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败'); return value;
}
function jsonPost(url,data) { return request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); }
async function begin(url,body,isJSON=false) {
  notice(); model=null; busy(true,'正在读取原生文字、照片和模板结构');
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
    if (job.status==='complete') { model=job.model; renderResult(job); showPage('result'); return; }
    if (job.generation_error) { notice(job.generation_error); return; }
    model=job.model; renderReview(); showPage('review');
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
    return `<div class="panel expert-card" data-expert="${i}"><div>${current?`<img class="expert-photo" src="${current.thumbnail}" alt="${escapeHTML(e.name)}资料照片">`:'<div class="expert-photo"></div>'}<div class="expert-source">${escapeHTML(e.source)}</div>${e.photos.length>1?`<select aria-label="选择照片" data-expert-field="photo">${e.photos.map((p,j)=>`<option value="${escapeHTML(p.image)}" ${p.image===e.photo?'selected':''}>候选照片${j+1}</option>`).join('')}</select>`:''}${!e.photo_confirmed?'<label class="hint"><input type="checkbox" data-confirm-photo>已确认当前照片</label>':''}</div><div class="expert-fields"><label>姓名<input data-expert-field="name" value="${escapeHTML(e.name)}"></label><label>会议展示单位<input data-person-field="hospital" value="${escapeHTML(detail.hospital||e.hospital)}"></label><label>会议展示称呼<input data-person-field="display_title" value="${escapeHTML(detail.display_title||'')}"></label><label class="bio">简介原文 · 每行一条，生成时整理为固定8行<textarea data-expert-field="bio">${escapeHTML(e.bio.join('\n'))}</textarea></label></div></div>`;
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
async function init() {
  try {config=await request('/api/config');}catch(error){notice(error.message);return;}
  $('#samples').innerHTML=config.samples.length?'<span>使用项目中的现有资料体验</span>'+config.samples.map(s=>`<button data-sample="${s}">${s==='input'?'9月7日临床营养会议':'8月13日胸部肿瘤会议'}</button>`).join(''):'';
  $$('[data-sample]').forEach(b=>b.onclick=()=>begin('/api/sample',{name:b.dataset.sample},true));
  $$('input[type=file]').forEach(input=>input.onchange=()=>{input.parentElement.querySelector('.file-state').textContent=input.files.length?`${input.files.length}个文件 · ${input.files[0].name}`:'点击选择文件';});
  $('#upload-form').onsubmit=event=>{event.preventDefault();begin('/api/analyze',new FormData(event.target));};
  $('#back-upload').onclick=()=>showPage('upload');$('#back-review').onclick=()=>{renderReview();showPage('review');};
  $$('.step').forEach(button=>button.onclick=()=>{if(button.dataset.step==='upload'||(button.dataset.step==='review'&&model))showPage(button.dataset.step);});
  $$('.tabs button').forEach(button=>button.onclick=()=>{$$('.tabs button').forEach(b=>b.classList.toggle('selected',b===button));$$('.tab-panel').forEach(p=>p.hidden=p.id!==button.dataset.tab);if(button.dataset.tab==='agenda-panel')renderAgenda();});
  $('#add-event').onclick=()=>{model.agenda.events.push({time:'',kind:'talk',title:'',people:[],hosts:[]});renderAgenda();};
  $('#generate').onclick=async()=>{
    notice(); model.options.repeat_chairs=$('#option-repeat').checked;model.options.include_topics=$('#option-topics').checked;model.options.draft=$('#option-draft').checked;
    busy(true,'正在复制模板页面并写入本次会议资料');
    try {await jsonPost(`/api/jobs/${jobId}/generate`,model);poll();}catch(error){busy(false);notice(error.message);}
  };
}
init();
