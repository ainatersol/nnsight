from __future__ import annotations
import zlib
import msgspec
import json
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Union

from pydantic import BaseModel, ConfigDict, TypeAdapter, field_serializer

from .. import NNsight
from .format.types import *

if TYPE_CHECKING:
    from ..contexts.backends.RemoteBackend import RemoteMixin

OBJECT_TYPES = Union[SessionType, TracerType, SessionModel, TracerModel]


class RequestModel(BaseModel):

    model_config = ConfigDict(
        arbitrary_types_allowed=True, protected_namespaces=()
    )

    object: str | OBJECT_TYPES
    model_key: str

    id: str = None
    received: datetime = None

    session_id: Optional[str] = None

    @field_serializer("object")
    def serialize_object(
        self, object: Union[SessionType, TracerType, SessionModel, TracerModel]
    ) -> str:

        if isinstance(object, str):
            return object

        return object.model_dump_json()

    def deserialize(self, model: NNsight) -> "RemoteMixin":

        handler = DeserializeHandler(model=model)

        object: OBJECT_TYPES = TypeAdapter(
            OBJECT_TYPES, config=RequestModel.model_config
        ).validate_python(json.loads(self.object))

        return object.deserialize(handler)

class StreamValueModel(BaseModel):
    
    model_config = ConfigDict(
        arbitrary_types_allowed=True, protected_namespaces=()
    )
    
    value: ValueTypes
    
    @model_serializer(mode="wrap")
    def serialize_model(self, handler):

        data = handler(self)
        
        data = msgspec.json.encode(data)
        
        data = zlib.compress(data)

        return data
    
    @classmethod
    def deserialize(cls, data:bytes | Dict, model:NNsight, _msgspec:bool=False, _zlib:bool=False):
        
        if _zlib:
            
            data = zlib.decompress(data)
            
        if _msgspec:
            
            data = msgspec.json.decode(data)
            
        data = StreamValueModel(**data)
            
        handler = DeserializeHandler(model=model)
        
        return try_deserialize(data.value, handler)
                
        