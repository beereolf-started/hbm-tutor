// ╔══════════════════════════════════════════════════════════════╗
// ║  HBM РЕПЕТИТОР — api.js                                     ║
// ║  Общий модуль: токен, авторизованный fetch, logout           ║
// ╚══════════════════════════════════════════════════════════════╝

const API = '/api';

function getToken() {
  return localStorage.getItem('hbm_token');
}

function getRole() {
  return localStorage.getItem('hbm_role');
}

function getUserName() {
  return localStorage.getItem('hbm_name');
}

// Проверка авторизации — редирект на логин если нет токена
function requireAuth(allowedRoles) {
  const token = getToken();
  const role  = getRole();
  if (!token) {
    location.href = '/login.html';
    return false;
  }
  if (allowedRoles && !allowedRoles.includes(role)) {
    location.href = '/login.html';
    return false;
  }
  return true;
}

function logout() {
  localStorage.removeItem('hbm_token');
  localStorage.removeItem('hbm_role');
  localStorage.removeItem('hbm_name');
  location.href = '/login.html';
}

// Fetch с авторизацией. При 401 — редирект на логин.
async function apiFetch(url, options = {}) {
  const token = getToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers['Authorization'] = 'Bearer ' + token;

  // Не ставить Content-Type для FormData (браузер сам поставит boundary)
  if (!(options.body instanceof FormData) && !headers['Content-Type'] && options.body) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(API + url, { ...options, headers });

  if (res.status === 401) {
    logout();
    throw new Error('Не авторизован');
  }
  return res;
}

// Шорткаты
async function apiGet(url) {
  const res = await apiFetch(url);
  return res.json();
}

async function apiPost(url, data) {
  const res = await apiFetch(url, {
    method: 'POST',
    body: JSON.stringify(data),
  });
  return res.json();
}

async function apiPatch(url, data) {
  const res = await apiFetch(url, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
  return res.json();
}

async function apiDelete(url) {
  await apiFetch(url, { method: 'DELETE' });
}

async function apiUpload(url, file) {
  const formData = new FormData();
  formData.append('file', file);
  const res = await apiFetch(url, { method: 'POST', body: formData });
  return res.json();
}
