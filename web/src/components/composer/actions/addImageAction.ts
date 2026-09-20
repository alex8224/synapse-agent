/**
 * The composer action menu's "add image" row.
 *
 * The row does not upload anything itself: it asks the host to open the image
 * picker (`context.pickImages`), and the files the reader chooses take the same
 * `handleFiles` path a paste or a drop already takes.  Keeping the picker in the
 * host is what keeps this action free of the store and of the DOM.
 *
 * The row paints its own mark, so the host needs no glyph table for it.
 */
import React from 'react';
import { Image20Regular } from '@fluentui/react-icons';
import type { ComposerActionDefinition } from './contract.ts';

export const ADD_IMAGE_ACTION_ID = 'add-image';

export const addImageAction: ComposerActionDefinition<React.ReactNode> = {
  id: ADD_IMAGE_ACTION_ID,
  label: '添加图片',
  detail: '从本地选择图片，随下一条消息一起发送',
  icon: React.createElement(Image20Regular, {
    'aria-hidden': true,
    className: 'shrink-0 text-gray-500',
  }),
  run: ({ pickImages }) => {
    pickImages();
  },
};
