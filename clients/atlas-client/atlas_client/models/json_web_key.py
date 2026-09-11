from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="JSONWebKey")



@_attrs_define
class JSONWebKey:
    """ One public Ed25519 signature key.

        Attributes:
            alg (Literal['EdDSA']):
            crv (Literal['Ed25519']):
            kid (str):
            kty (Literal['OKP']):
            use (Literal['sig']):
            x (str):
            key_ops (list[Literal['verify']] | Unset):
     """

    alg: Literal['EdDSA']
    crv: Literal['Ed25519']
    kid: str
    kty: Literal['OKP']
    use: Literal['sig']
    x: str
    key_ops: list[Literal['verify']] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        alg = self.alg

        crv = self.crv

        kid = self.kid

        kty = self.kty

        use = self.use

        x = self.x

        key_ops: list[Literal['verify']] | Unset = UNSET
        if not isinstance(self.key_ops, Unset):
            key_ops = self.key_ops




        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "alg": alg,
            "crv": crv,
            "kid": kid,
            "kty": kty,
            "use": use,
            "x": x,
        })
        if key_ops is not UNSET:
            field_dict["key_ops"] = key_ops

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        alg = cast(Literal['EdDSA'] , d.pop("alg"))
        if alg != 'EdDSA':
            raise ValueError(f"alg must match const 'EdDSA', got '{alg}'")

        crv = cast(Literal['Ed25519'] , d.pop("crv"))
        if crv != 'Ed25519':
            raise ValueError(f"crv must match const 'Ed25519', got '{crv}'")

        kid = d.pop("kid")

        kty = cast(Literal['OKP'] , d.pop("kty"))
        if kty != 'OKP':
            raise ValueError(f"kty must match const 'OKP', got '{kty}'")

        use = cast(Literal['sig'] , d.pop("use"))
        if use != 'sig':
            raise ValueError(f"use must match const 'sig', got '{use}'")

        x = d.pop("x")

        _key_ops = d.pop("key_ops", UNSET)
        key_ops: list[Literal['verify']] | Unset = UNSET
        if _key_ops is not UNSET:
            key_ops = []
            for key_ops_item_data in _key_ops:
                key_ops_item = cast(Literal['verify'] , key_ops_item_data)
                if key_ops_item != 'verify':
                    raise ValueError(f"key_ops_item must match const 'verify', got '{key_ops_item}'")
                key_ops.append(key_ops_item)


        json_web_key = cls(
            alg=alg,
            crv=crv,
            kid=kid,
            kty=kty,
            use=use,
            x=x,
            key_ops=key_ops,
        )


        json_web_key.additional_properties = d
        return json_web_key

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
