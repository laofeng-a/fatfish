"""Outbound note rules reject fixed regressions without banning normal language."""
import unittest

from internal_note_filter import internal_note_reason


class InternalNoteFilterTests(unittest.TestCase):
    def reason(self,text,**extra):
        return internal_note_reason(text,background=True,origin='send_tool',**extra)

    def test_exact_accident_and_known_round_meta_statements(self):
        for text in ['这一轮不打扰他','这一轮不打扰他。','本轮没有打扰jg。',
                     '本轮保持静默，等下次机会。','这一轮先安静结束。',
                     '这次不外发。','本次不发送消息。','本轮决定：不再打扰他。',
                     '本轮不向jg发送消息。','先等jg开口。','等jg开口。','接下来就先等他开口。',
                     '接下来就先保持安静，等他开口。',
                     '本次内部记录：留在日记里。']:
            with self.subTest(text=text):self.assertEqual(self.reason([text]),'explicit_internal_note')

    def test_existing_internal_record_heading_remains_compatible(self):
        for text in ['内部记录：这次不发送。','内部记录\n这次不发送。',
                     '## 内部记录（不外发）：\n完成。','**内部记录（静默）**：完成。',
                     '__内部记录__：完成。']:
            with self.subTest(text=text):self.assertEqual(self.reason([text]),'explicit_internal_note')

    def test_supported_presentation_does_not_hide_standalone_note(self):
        for text in ['（这一轮不打扰他。）','- 本轮保持静默。','**这一轮不打扰他**。',
                     '一句正常正文。\n本轮不外发。']:
            with self.subTest(text=text):self.assertEqual(self.reason([text]),'explicit_internal_note')

    def test_direct_recipient_language_and_ordinary_quiet_words_are_allowed(self):
        for text in ['我先不打扰你啦，晚安。','这一轮不打扰你了，晚安。',
                     '陪你安静坐一会儿。','我现在很安静，但还想听你说。',
                     '你先休息，醒了再聊。','我想听你接着说。',
                     '本轮保持静默是什么意思？','内部记录应该如何查看？']:
            with self.subTest(text=text):self.assertIsNone(self.reason([text]))

    def test_normal_foreground_and_other_origins_are_not_filtered(self):
        text=['这一轮不打扰他。']
        self.assertIsNone(internal_note_reason(text,background=False,origin='send_tool'))
        self.assertIsNone(internal_note_reason(text,background=True,origin='auto_final'))
        self.assertIsNone(internal_note_reason(text,background=True,origin='internal_final'))
        self.assertIsNone(internal_note_reason(text))

    def test_quoted_and_explanatory_discussion_is_not_a_note(self):
        for text in ['“这一轮不打扰他”是我们讨论的错误。',
                     '"本轮保持静默"不应发给用户。',
                     '`这一轮不打扰他` 是一条内部结论。',
                     '为什么她总说“这一轮不打扰他”？',
                     '> 这一轮不打扰他。\n\n这句话是引用。',
                     '例如：\n这一轮不打扰他。\n\n这是示例。',
                     '下面解释：\n本轮保持静默。']:
            with self.subTest(text=text):self.assertIsNone(self.reason([text]))

    def test_code_fences_are_ignored_but_actual_following_note_is_guarded(self):
        for text in ['```text\n这一轮不打扰他。\n```',
                     '~~~\n内部记录：x\n~~~',
                     '```python\nx="本轮保持静默"\n```\n正常解释。']:
            with self.subTest(text=text):self.assertIsNone(self.reason([text]))
        self.assertEqual(self.reason(['```text\n本轮保持静默\n```\n\n本轮不外发。']),'explicit_internal_note')

    def test_all_parts_are_checked_and_non_text_values_are_ignored(self):
        self.assertEqual(self.reason(['正常第一条。','本轮保持静默。']),'explicit_internal_note')
        self.assertEqual(self.reason(['正常图片说明。','这一轮不打扰他。']),'explicit_internal_note')
        self.assertIsNone(self.reason([None,{'text':'not a string'},'晚安。']))
        self.assertIsNone(self.reason(None))
        self.assertIsNone(self.reason([]))

    def test_single_string_input_does_not_iterate_characters(self):
        self.assertEqual(self.reason('这一轮不打扰他。'),'explicit_internal_note')

    def test_alternative_natural_wording_is_not_claimed_to_be_classified(self):
        # The guard intentionally does not pretend to understand all paraphrases.
        self.assertIsNone(self.reason(['那我就把刚才想到的事留在心里。']))


if __name__=='__main__':unittest.main(verbosity=2)
