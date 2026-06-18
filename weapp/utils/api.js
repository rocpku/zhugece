// utils/api.js — 诸葛策 API 封装

const BASE_URL = 'http://localhost:8080'

function getToken() {
  return wx.getStorageSync('token') || ''
}

function setToken(token, username) {
  wx.setStorageSync('token', token)
  wx.setStorageSync('username', username || '')
}

function clearToken() {
  wx.removeStorageSync('token')
  wx.removeStorageSync('username')
}

function request(method, path, data) {
  return new Promise((resolve, reject) => {
    const header = { 'Content-Type': 'application/json' }
    const token = getToken()
    if (token) {
      header['Authorization'] = 'Bearer ' + token
    }
    wx.request({
      url: BASE_URL + path,
      method,
      header,
      data,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          reject({ code: res.statusCode, msg: res.data?.detail || '请求失败' })
        }
      },
      fail(err) {
        reject({ code: -1, msg: '网络连接失败，请确认服务器运行中' })
      },
    })
  })
}

// 获取/设置本地保存的 dev 用户名
function getSavedUsername() {
  return wx.getStorageSync('dev_username') || ''
}

function setSavedUsername(name) {
  wx.setStorageSync('dev_username', name || '')
}

// 微信登录（带 fallback 到 dev-login）
function login(username) {
  return new Promise((resolve, reject) => {
    const doDevLogin = () => {
      request('POST', '/api/dev-login', { username: username || '' }).then(resolve).catch(reject)
    }
    wx.login({
      success(loginRes) {
        if (loginRes.code) {
          request('POST', '/api/wx-login', { code: loginRes.code })
            .then(resolve)
            .catch(doDevLogin)
        } else {
          doDevLogin()
        }
      },
      fail: doDevLogin,
    })
  })
}

// 直接 dev-login（跳过微信，用于切换账号）
function devLogin(username) {
  return request('POST', '/api/dev-login', { username: username || '' })
}

// 对话列表
function getConversations() {
  return request('GET', '/api/conversations')
}

// 创建新对话
function createConversation() {
  return request('POST', '/api/conversations')
}

// 删除对话
function deleteConversation(id) {
  return request('DELETE', '/api/conversations/' + id)
}

// 获取对话历史
function getHistory(convId) {
  return request('GET', '/api/conversations/' + convId + '/history')
}

// 发送消息（非流式）
function sendMessage(message, convId) {
  return request('POST', '/api/send', { message, conversation_id: convId || 0 })
}

module.exports = {
  BASE_URL,
  getToken,
  setToken,
  clearToken,
  login,
  devLogin,
  getSavedUsername,
  setSavedUsername,
  getConversations,
  createConversation,
  deleteConversation,
  getHistory,
  sendMessage,
}
