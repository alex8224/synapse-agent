import assert from 'node:assert/strict';
import test from 'node:test';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';
import { lastUserMessageText, lastAssistantReplyText } from '../src/stores/sessionPreview.ts';

test('sessionPreview returns empty string for empty or missing messages', () => {
  assert.equal(lastUserMessageText(null), '');
  assert.equal(lastUserMessageText(undefined), '');
  assert.equal(lastUserMessageText([]), '');
  assert.equal(lastAssistantReplyText(null), '');
  assert.equal(lastAssistantReplyText(undefined), '');
  assert.equal(lastAssistantReplyText([]), '');
});

test('sessionPreview extracts last user message across multiple turns', () => {
  const messages: TranscriptMessage[] = [
    { id: '1', type: 'user', content: '第一轮提问', timestamp: '12:00' },
    { id: '2', type: 'assistant', content: '第一轮回复', timestamp: '12:01' },
    { id: '3', type: 'user', content: '第二轮最新提问', timestamp: '12:02' },
  ];

  assert.equal(lastUserMessageText(messages), '第二轮最新提问');
});

test('sessionPreview ignores whitespace-only user messages', () => {
  const messages: TranscriptMessage[] = [
    { id: '1', type: 'user', content: '有效提问', timestamp: '12:00' },
    { id: '2', type: 'user', content: '   ', timestamp: '12:01' },
  ];

  assert.equal(lastUserMessageText(messages), '有效提问');
});

test('sessionPreview extracts last assistant reply belonging to the final turn', () => {
  const messages: TranscriptMessage[] = [
    { id: '1', type: 'user', content: '第一轮提问', timestamp: '12:00' },
    { id: '2', type: 'assistant', content: '第一轮回复', timestamp: '12:01' },
    { id: '3', type: 'user', content: '第二轮提问', timestamp: '12:02' },
    { id: '4', type: 'thought', content: '思考中...', timestamp: '12:03' },
    { id: '5', type: 'assistant', content: '第二轮中间回答', timestamp: '12:04' },
    { id: '6', type: 'assistant', content: '第二轮最终回答', timestamp: '12:05' },
  ];

  assert.equal(lastAssistantReplyText(messages), '第二轮最终回答');
});

test('sessionPreview returns empty when current turn has not produced an assistant reply yet', () => {
  const messages: TranscriptMessage[] = [
    { id: '1', type: 'user', content: '第一轮提问', timestamp: '12:00' },
    { id: '2', type: 'assistant', content: '第一轮回复', timestamp: '12:01' },
    { id: '3', type: 'user', content: '第二轮新提问', timestamp: '12:02' },
    { id: '4', type: 'thought', content: '思考中...', timestamp: '12:03' },
  ];

  // 第二轮尚未产生 assistant 回复，不应泄漏第一轮的回复
  assert.equal(lastAssistantReplyText(messages), '');
});
