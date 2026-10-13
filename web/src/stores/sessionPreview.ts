import type { TranscriptMessage } from './historyMapper.ts';

/**
 * 提取会话转录中最后一次用户提交的消息文本。
 * 若无用户消息，返回空字符串。
 */
export function lastUserMessageText(messages: readonly TranscriptMessage[] | null | undefined): string {
  if (!messages || messages.length === 0) return '';
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.type === 'user' && msg.content && msg.content.trim() !== '') {
      return msg.content.trim();
    }
  }
  return '';
}

/**
 * 提取最后一个轮次中模型的最新回复文本（最后一条非空 assistant 消息）。
 * 仅在最后一个用户消息之后查找属于该轮的回复；若该轮尚未生成回复，返回空字符串。
 */
export function lastAssistantReplyText(messages: readonly TranscriptMessage[] | null | undefined): string {
  if (!messages || messages.length === 0) return '';

  let lastUserIndex = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].type === 'user') {
      lastUserIndex = i;
      break;
    }
  }

  const startIndex = lastUserIndex >= 0 ? lastUserIndex + 1 : 0;

  for (let i = messages.length - 1; i >= startIndex; i--) {
    const msg = messages[i];
    if (msg.type === 'assistant' && msg.content && msg.content.trim() !== '') {
      return msg.content.trim();
    }
  }

  return '';
}
