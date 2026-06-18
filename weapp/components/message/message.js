// components/message/message.js
const { markdownToHtml } = require('../../utils/util')

Component({
  properties: {
    role: { type: String, value: 'agent' },
    content: { type: String, value: '' },
  },

  data: {
    formattedContent: '',
  },

  observers: {
    content(val) {
      if (this.properties.role === 'agent') {
        this.setData({ formattedContent: markdownToHtml(val || '') })
      }
    },
  },

  lifetimes: {
    attached() {
      if (this.properties.role === 'agent') {
        this.setData({ formattedContent: markdownToHtml(this.properties.content || '') })
      }
    },
  },
})
