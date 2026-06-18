// app.js
App({
  globalData: {
    token: '',
    username: '',
  },

  onLaunch() {
    // 检查本地缓存的登录态
    const token = wx.getStorageSync('token')
    const username = wx.getStorageSync('username')
    if (token) {
      this.globalData.token = token
      this.globalData.username = username
    }
  },
})
