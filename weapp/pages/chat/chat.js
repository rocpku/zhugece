// pages/chat/chat.js
const api = require('../../utils/api')
const { formatTime } = require('../../utils/util')

Page({
  data: {
    messages: [],
    inputValue: '',
    loading: false,
    currentConvId: 0,
    conversations: [],
    sidebarOpen: false,
    scrollToId: 'bottom',
    statusBarHeight: 0,
    navBarHeight: 88,
    // 账号相关
    currentUsername: '',
    showAccountInput: false,
    accountInputValue: '',
  },

  onLoad() {
    const sysInfo = wx.getSystemInfoSync()
    this.setData({
      statusBarHeight: sysInfo.statusBarHeight,
      navBarHeight: sysInfo.statusBarHeight + 44,
      currentUsername: api.getSavedUsername() || 'dev_user',
    })
    this.loginAndInit()
  },

  // ── 登录并初始化 ──
  async loginAndInit() {
    const token = api.getToken()
    if (!token) {
      try {
        wx.showLoading({ title: '登录中...', mask: true })
        const username = api.getSavedUsername()
        const res = await api.login(username)
        api.setToken(res.token, res.username)
        this.setData({ currentUsername: res.username || username || 'dev_user' })
        wx.hideLoading()
      } catch (e) {
        wx.hideLoading()
        wx.showToast({ title: '登录失败: ' + (e.msg || '网络错误'), icon: 'none', duration: 3000 })
        return
      }
    }
    this.loadConversations()
  },

  // ── 账号切换 ──
  showAccountSwitch() {
    this.setData({
      showAccountInput: true,
      accountInputValue: this.data.currentUsername,
    })
  },

  onAccountInput(e) {
    this.setData({ accountInputValue: e.detail.value })
  },

  cancelAccountSwitch() {
    this.setData({ showAccountInput: false, accountInputValue: '' })
  },

  async confirmAccountSwitch() {
    const username = this.data.accountInputValue.trim()
    if (!username) return

    wx.showLoading({ title: '切换账号...', mask: true })
    try {
      // 用新用户名重新登录
      const res = await api.devLogin(username)
      api.setToken(res.token, res.username)
      api.setSavedUsername(username)

      this.setData({
        currentUsername: username,
        showAccountInput: false,
        accountInputValue: '',
        messages: [],
        conversations: [],
        currentConvId: 0,
        sidebarOpen: false,
      })

      wx.hideLoading()
      wx.showToast({ title: '已切换到 ' + username, icon: 'success' })
      this.loadConversations()
    } catch (e) {
      wx.hideLoading()
      wx.showToast({ title: '切换失败: ' + (e.msg || '网络错误'), icon: 'none' })
    }
  },

  // ── 对话列表 ──
  async loadConversations(autoSwitch = true) {
    try {
      const convs = await api.getConversations()
      this.setData({ conversations: convs })
      if (autoSwitch && convs.length > 0) {
        this.switchToConversation(convs[0].id)
      }
    } catch (e) {
      console.error('加载对话失败', e)
    }
  },

  async switchToConversation(convId) {
    this.setData({ currentConvId: convId, messages: [] })
    try {
      const msgs = await api.getHistory(convId)
      this.setData({ messages: msgs })
      this.scrollToBottom()
    } catch (e) {
      console.error('加载历史失败', e)
    }
  },

  switchConversation(e) {
    const id = e.currentTarget.dataset.id
    this.setData({ sidebarOpen: false })
    if (id !== this.data.currentConvId) {
      this.switchToConversation(id)
    }
  },

  async newConversation() {
    this.setData({ sidebarOpen: false })
    try {
      const conv = await api.createConversation()
      const convs = this.data.conversations
      convs.unshift(conv)
      this.setData({ conversations: convs, currentConvId: conv.id, messages: [] })
    } catch (e) {
      console.error('创建对话失败', e)
    }
  },

  toggleSidebar() {
    this.setData({ sidebarOpen: !this.data.sidebarOpen })
  },

  closeSidebar() {
    this.setData({ sidebarOpen: false })
  },

  // 阻止侧栏打开时背景滚动穿透
  preventMove() {
    return
  },

  // ── 推荐问题点击 ──
  onSuggestion(e) {
    const text = e.currentTarget.dataset.text
    if (!text || this.data.loading) return
    // 直接设置输入值并触发发送
    this.data.inputValue = text
    this.onSend()
  },

  // ── 输入 ──
  onInput(e) {
    this.setData({ inputValue: e.detail.value })
  },

  // ── 发送消息 ──
  async onSend() {
    const text = this.data.inputValue.trim()
    if (!text || this.data.loading) return

    this.setData({ inputValue: '' })

    // 没有当前对话则自动创建
    let convId = this.data.currentConvId
    if (!convId) {
      try {
        const conv = await api.createConversation()
        convId = conv.id
        const convs = this.data.conversations
        convs.unshift(conv)
        this.setData({ conversations: convs, currentConvId: convId })
      } catch (e) {
        wx.showToast({ title: '创建对话失败', icon: 'none' })
        return
      }
    }

    // 添加用户消息到界面
    const msgs = this.data.messages
    msgs.push({ role: 'user', content: text })
    this.setData({ messages: msgs, loading: true })
    this.scrollToBottom()

    try {
      const res = await api.sendMessage(text, convId)

      // 添加 AI 回复
      msgs.push({ role: 'assistant', content: res.response || '' })
      this.setData({ messages: msgs, loading: false })
      this.scrollToBottom()

      // 刷新对话列表（标题可能变了），不自动切换对话
      this.loadConversations(false)
    } catch (e) {
      msgs.push({ role: 'assistant', content: '抱歉，连接失败：' + (e.msg || '请确认服务器正在运行') })
      this.setData({ messages: msgs, loading: false })
      this.scrollToBottom()
    }
  },

  scrollToBottom() {
    setTimeout(() => {
      this.setData({ scrollToId: 'bottom' })
    }, 100)
  },
})
