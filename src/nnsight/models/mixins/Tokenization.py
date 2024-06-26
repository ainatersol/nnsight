from typing import Any

from ... import NNsight
from ...util import wrap_object_as_module

class TokenizationMixin(NNsight):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self.tokenizer = wrap_object_as_module(self._tokenizer)

        self._envoy._add_envoy(self.tokenizer, "tokenizer")
    