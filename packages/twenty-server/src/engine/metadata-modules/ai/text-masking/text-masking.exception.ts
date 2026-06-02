import { type MessageDescriptor } from '@lingui/core';
import { msg } from '@lingui/core/macro';
import { assertUnreachable } from 'twenty-shared/utils';

import { CustomException } from 'src/utils/custom-exception';

export enum TextMaskingExceptionCode {
  AI_SERVICE_UNAVAILABLE = 'AI_SERVICE_UNAVAILABLE',
  INVALID_SESSION = 'INVALID_SESSION',
}

const getTextMaskingExceptionUserFriendlyMessage = (
  code: TextMaskingExceptionCode,
) => {
  switch (code) {
    case TextMaskingExceptionCode.AI_SERVICE_UNAVAILABLE:
      return msg`The AI service is unavailable. Please try again later.`;
    case TextMaskingExceptionCode.INVALID_SESSION:
      return msg`The provided masking session is invalid.`;
    default:
      assertUnreachable(code);
  }
};

export class TextMaskingException extends CustomException<TextMaskingExceptionCode> {
  constructor(
    message: string,
    code: TextMaskingExceptionCode,
    { userFriendlyMessage }: { userFriendlyMessage?: MessageDescriptor } = {},
  ) {
    super(message, code, {
      userFriendlyMessage:
        userFriendlyMessage ?? getTextMaskingExceptionUserFriendlyMessage(code),
    });
  }
}
