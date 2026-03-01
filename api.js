const API_BASE='';
function hdr(){const t=localStorage.getItem('token');return{'Content-Type':'application/json',...(t?{'Authorization':'Bearer '+t}:{})}}
async function api(method,url,body){
    const r=await fetch(API_BASE+url,{method,headers:hdr(),...(body?{body:JSON.stringify(body)}:{})});
    if(r.status===401){localStorage.clear();location.href='/login.html';return}
    if(r.status===204)return null;
    const j=await r.json();if(!r.ok)throw j;return j}
const get=u=>api('GET',u),post=(u,b)=>api('POST',u,b),patch=(u,b)=>api('PATCH',u,b),del=u=>api('DELETE',u);
async function upload(url,file){
    const f=new FormData();f.append('file',file);const t=localStorage.getItem('token');
    const r=await fetch(API_BASE+url,{method:'POST',headers:{'Authorization':'Bearer '+t},body:f});
    if(!r.ok)throw await r.json();return r.json()}
function role(){return localStorage.getItem('role')}
function logout(){localStorage.clear();location.href='/login.html'}
function requireAuth(allowed){if(!localStorage.getItem('token')){location.href='/login.html';return false}if(allowed&&!allowed.includes(role())){location.href='/login.html';return false}return true}
