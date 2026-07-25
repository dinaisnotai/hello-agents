<template>
  <a-card class="chat-card" :bordered="false">
    <template #title>
      <div class="chat-title">
        <span>行程助手</span>
        <a-tag color="blue">版本 {{ version }}</a-tag>
      </div>
    </template>

    <div ref="messageList" class="message-list">
      <a-empty v-if="!messages.length" description="还没有对话" />
      <div
        v-for="item in messages"
        :key="item.id"
        class="message-row"
        :class="item.role"
      >
        <div class="message-bubble">
          <small>{{ item.role === 'user' ? '你' : '助手' }}</small>
          <p>{{ item.content }}</p>
        </div>
      </div>
    </div>

    <a-textarea
      v-model:value="content"
      :rows="3"
      :maxlength="1000"
      placeholder="例如：预算改成 2000；安排轻松一点；加入颐和园"
      @keydown.ctrl.enter="submit"
    />
    <a-button
      type="primary"
      block
      class="send-button"
      :loading="loading"
      :disabled="!content.trim()"
      @click="submit"
    >
      发送修改
    </a-button>
    <p class="chat-hint">当前支持预算、节奏和必去景点修改</p>
  </a-card>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ConversationMessage } from '@/types'

const props = defineProps<{
  messages: ConversationMessage[]
  version: number
  loading: boolean
}>()

const emit = defineEmits<{
  send: [content: string]
}>()

const content = ref('')
const messageList = ref<HTMLElement>()

const scrollToLatest = async () => {
  await nextTick()
  if (messageList.value) {
    messageList.value.scrollTop = messageList.value.scrollHeight
  }
}

watch(() => props.messages.length, scrollToLatest, { immediate: true })

const submit = () => {
  const value = content.value.trim()
  if (!value || props.loading) return
  emit('send', value)
  content.value = ''
}
</script>

<style scoped>
.chat-card {
  position: sticky;
  top: 24px;
}

.chat-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.message-list {
  height: min(520px, 58vh);
  overflow-y: auto;
  padding: 4px 2px 12px;
}

.message-row {
  display: flex;
  margin-bottom: 12px;
}

.message-row.user {
  justify-content: flex-end;
}

.message-bubble {
  max-width: 88%;
  padding: 9px 12px;
  border-radius: 12px;
  background: #f2f4f7;
}

.message-row.user .message-bubble {
  color: #fff;
  background: #1677ff;
}

.message-bubble small {
  display: block;
  margin-bottom: 3px;
  opacity: 0.72;
}

.message-bubble p {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.send-button {
  margin-top: 10px;
}

.chat-hint {
  margin: 8px 0 0;
  color: #98a2b3;
  font-size: 12px;
  text-align: center;
}

@media (max-width: 991px) {
  .chat-card {
    position: static;
  }
}
</style>
