"""Minimal ONVIF SOAP client (Device / Media / PTZ) for Tapo cameras.

Only the handful of operations needed by this project are implemented, using
plain ``requests`` and WS-Security UsernameToken (PasswordDigest).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import requests

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
TIMG = "http://www.onvif.org/ver20/imaging/wsdl"
for _prefix, _uri in NS.items():  # readable prefixes when serializing elements back (SetVideoEncoderConfiguration)
    ET.register_namespace(_prefix, _uri)
_WSSE ="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class OnvifError(RuntimeError):
    """Raised on SOAP faults or transport errors.

    ``subcodes`` holds the local names of the SOAP fault code/subcodes
    (e.g. ``["Receiver", "Action", "TooManyPresets"]``); empty for transport errors.
    """

    def __init__(self, message: str, subcodes: list[str] | None = None):
        super().__init__(message)
        self.subcodes = subcodes or []

    def has_subcode(self, name: str) -> bool:
        return name in self.subcodes


# '&' not starting an entity reference (Tapo returns preset names unescaped)
_BARE_AMP = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


@dataclass
class PtzVector:
    pan: float
    tilt: float


@dataclass
class Preset:
    token: str
    name: str


@dataclass
class Profile:
    token: str
    name: str
    has_ptz: bool
    video_source_token: str = ""
    encoder_token: str = ""


@dataclass
class OnvifCamera:
    host: str
    user: str
    password: str
    port: int = 2020
    timeout: float = 10.0
    _clock_offset: dt.timedelta = field(default=dt.timedelta(0), init=False)
    _xaddrs: dict[str, str] = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------ core
    @property
    def device_url(self) -> str:
        return f"http://{self.host}:{self.port}/onvif/device_service"

    def _security_header(self) -> str:
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self._clock_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        return (
            f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}" s:mustUnderstand="1">'
            "<wsse:UsernameToken>"
            f"<wsse:Username>{escape(self.user)}</wsse:Username>"
            f'<wsse:Password Type="{_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created>"
            "</wsse:UsernameToken></wsse:Security>"
        )

    def call(self, url: str, body: str, *, auth: bool = True) -> ET.Element:
        """Send a SOAP request and return the first child of the SOAP Body."""
        ns_decl = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
        header = f"<s:Header>{self._security_header()}</s:Header>" if auth else ""
        envelope = f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {ns_decl}>{header}<s:Body>{body}</s:Body></s:Envelope>'
        try:
            resp = requests.post(
                url,
                data=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OnvifError(f"transport error: {e}") from e
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            try:
                root = ET.fromstring(_BARE_AMP.sub(b"&amp;", resp.content))
            except ET.ParseError as e:
                raise OnvifError(f"HTTP {resp.status_code}, malformed XML response: {e}") from e
        body_el = root.find("s:Body", NS)
        if body_el is None or len(body_el) == 0:
            raise OnvifError(f"HTTP {resp.status_code}, empty SOAP body")
        first = body_el[0]
        if first.tag == f"{{{NS['s']}}}Fault":
            reason = " ".join(t.strip() for t in first.itertext() if t.strip())
            subcodes = [
                (v.text or "").strip().split(":")[-1]
                for v in first.iterfind(".//s:Code//s:Value", NS)
            ]
            raise OnvifError(f"SOAP fault (HTTP {resp.status_code}): {reason}", subcodes)
        return first

    # ---------------------------------------------------------------- device
    def sync_clock(self) -> dt.timedelta:
        """Align the WS-Security timestamp with the camera clock (no auth needed)."""
        r = self.call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
        utc = r.find(".//tt:UTCDateTime", NS)
        if utc is not None:
            d, t = utc.find("tt:Date", NS), utc.find("tt:Time", NS)
            cam = dt.datetime(
                int(d.findtext("tt:Year", namespaces=NS)),
                int(d.findtext("tt:Month", namespaces=NS)),
                int(d.findtext("tt:Day", namespaces=NS)),
                int(t.findtext("tt:Hour", namespaces=NS)),
                int(t.findtext("tt:Minute", namespaces=NS)),
                int(t.findtext("tt:Second", namespaces=NS)),
                tzinfo=dt.timezone.utc,
            )
            self._clock_offset = cam - dt.datetime.now(dt.timezone.utc)
        return self._clock_offset

    def get_device_information(self) -> dict[str, str]:
        r = self.call(self.device_url, "<tds:GetDeviceInformation/>")
        return {el.tag.split("}")[1]: (el.text or "") for el in r}

    def get_capabilities(self) -> dict[str, str]:
        """Return service name -> XAddr (e.g. {'Media': ..., 'PTZ': ...})."""
        r = self.call(self.device_url, "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
        caps = r.find("tds:Capabilities", NS)
        out = {}
        if caps is not None:
            for svc in caps:
                xaddr = svc.findtext("tt:XAddr", namespaces=NS)
                if xaddr:
                    out[svc.tag.split("}")[1]] = xaddr
        self._xaddrs = out
        return out

    def _xaddr(self, name: str) -> str:
        if not self._xaddrs:
            self.get_capabilities()
        if name not in self._xaddrs:
            raise OnvifError(f"camera does not advertise the {name} service")
        return self._xaddrs[name]

    # ----------------------------------------------------------------- media
    def get_profiles(self) -> list[Profile]:
        r = self.call(self._xaddr("Media"), "<trt:GetProfiles/>")
        out = []
        for p in r.findall("trt:Profiles", NS):
            enc = p.find("tt:VideoEncoderConfiguration", NS)
            out.append(
                Profile(
                    token=p.get("token", ""),
                    name=p.findtext("tt:Name", default="", namespaces=NS),
                    has_ptz=p.find("tt:PTZConfiguration", NS) is not None,
                    video_source_token=p.findtext(
                        "tt:VideoSourceConfiguration/tt:SourceToken", default="", namespaces=NS
                    ),
                    encoder_token=enc.get("token", "") if enc is not None else "",
                )
            )
        return out

    def get_video_encoder_configuration(self, token: str) -> ET.Element:
        """Return the tt:VideoEncoderConfiguration element (as trt:Configuration)."""
        body = (
            "<trt:GetVideoEncoderConfiguration>"
            f"<trt:ConfigurationToken>{escape(token)}</trt:ConfigurationToken>"
            "</trt:GetVideoEncoderConfiguration>"
        )
        cfg = self.call(self._xaddr("Media"), body).find("trt:Configuration", NS)
        if cfg is None:
            raise OnvifError(f"no video encoder configuration {token!r}")
        return cfg

    def set_video_encoder_configuration(self, cfg: ET.Element) -> None:
        """Write back a (modified) element returned by get_video_encoder_configuration()."""
        xml = ET.tostring(cfg, encoding="unicode")  # self-contained: declares its namespaces
        body = (
            f"<trt:SetVideoEncoderConfiguration>{xml}"
            "<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>"
        )
        self.call(self._xaddr("Media"), body)

    def get_encoder_resolutions(self, profile: str) -> list[tuple[int, int]]:
        body = (
            "<trt:GetVideoEncoderConfigurationOptions>"
            f"<trt:ProfileToken>{escape(profile)}</trt:ProfileToken>"
            "</trt:GetVideoEncoderConfigurationOptions>"
        )
        opts = self.call(self._xaddr("Media"), body).find("trt:Options", NS)
        res: list[tuple[int, int]] = []
        if opts is not None:
            for r in opts.iterfind("tt:H264/tt:ResolutionsAvailable", NS):
                wh = (int(r.findtext("tt:Width", namespaces=NS)), int(r.findtext("tt:Height", namespaces=NS)))
                if wh not in res:
                    res.append(wh)
        return res

    # --------------------------------------------------------------- imaging
    def get_imaging_settings(self, source: str) -> dict[str, float]:
        """Return the numeric top-level imaging settings (Brightness, ColorSaturation, ...)."""
        body = (
            f'<timg:GetImagingSettings xmlns:timg="{TIMG}">'
            f"<timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken></timg:GetImagingSettings>"
        )
        s = self.call(self._xaddr("Imaging"), body).find(f"{{{TIMG}}}ImagingSettings")
        out: dict[str, float] = {}
        if s is not None:
            for el in s:
                try:
                    out[el.tag.split("}")[1]] = float(el.text or "")
                except ValueError:
                    pass
        return out

    def set_imaging_settings(self, source: str, values: dict[str, float]) -> None:
        inner = "".join(f"<tt:{k}>{v:g}</tt:{k}>" for k, v in values.items())
        body = (
            f'<timg:SetImagingSettings xmlns:timg="{TIMG}">'
            f"<timg:VideoSourceToken>{escape(source)}</timg:VideoSourceToken>"
            f"<timg:ImagingSettings>{inner}</timg:ImagingSettings>"
            "<timg:ForcePersistence>true</timg:ForcePersistence></timg:SetImagingSettings>"
        )
        self.call(self._xaddr("Imaging"), body)

    def get_stream_uri(self, profile: str) -> str:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(profile)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        return self.call(self._xaddr("Media"), body).findtext(".//tt:Uri", default="", namespaces=NS)

    def get_snapshot_uri(self, profile: str) -> str:
        body = f"<trt:GetSnapshotUri><trt:ProfileToken>{escape(profile)}</trt:ProfileToken></trt:GetSnapshotUri>"
        return self.call(self._xaddr("Media"), body).findtext(".//tt:Uri", default="", namespaces=NS)

    # ------------------------------------------------------------------- ptz
    def get_ptz_spaces(self) -> dict[str, list[str]]:
        """Return supported PTZ coordinate spaces per kind (from GetNodes)."""
        r = self.call(self._xaddr("PTZ"), "<tptz:GetNodes/>")
        spaces: dict[str, list[str]] = {}
        for sp in r.iterfind(".//tt:SupportedPTZSpaces", NS):
            for child in sp:
                kind = child.tag.split("}")[1]
                uri = child.findtext("tt:URI", default="", namespaces=NS)
                spaces.setdefault(kind, []).append(uri)
        return spaces

    def get_presets(self, profile: str) -> list[Preset]:
        body = f"<tptz:GetPresets><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GetPresets>"
        r = self.call(self._xaddr("PTZ"), body)
        return [
            Preset(token=p.get("token", ""), name=p.findtext("tt:Name", default="", namespaces=NS))
            for p in r.findall("tptz:Preset", NS)
        ]

    def goto_preset(self, profile: str, token: str) -> None:
        body = (
            f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:GotoPreset>"
        )
        self.call(self._xaddr("PTZ"), body)

    def set_preset(self, profile: str, name: str, token: str | None = None) -> str:
        """Store the current position. With ``token``, overwrite that preset."""
        tok = f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken>" if token else ""
        body = (
            f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetName>{escape(name)}</tptz:PresetName>{tok}</tptz:SetPreset>"
        )
        return self.call(self._xaddr("PTZ"), body).findtext("tptz:PresetToken", default="", namespaces=NS)

    def remove_preset(self, profile: str, token: str) -> None:
        body = (
            f"<tptz:RemovePreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f"<tptz:PresetToken>{escape(token)}</tptz:PresetToken></tptz:RemovePreset>"
        )
        self.call(self._xaddr("PTZ"), body)

    def get_status(self, profile: str) -> PtzVector | None:
        body = f"<tptz:GetStatus><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GetStatus>"
        pt = self.call(self._xaddr("PTZ"), body).find(".//tt:Position/tt:PanTilt", NS)
        if pt is None:
            return None
        return PtzVector(float(pt.get("x", "nan")), float(pt.get("y", "nan")))

    def continuous_move(self, profile: str, velocity: PtzVector, timeout_s: float | None = None) -> None:
        tmo = f"<tptz:Timeout>PT{timeout_s:g}S</tptz:Timeout>" if timeout_s else ""
        body = (
            f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f'<tptz:Velocity><tt:PanTilt x="{velocity.pan}" y="{velocity.tilt}"/></tptz:Velocity>'
            f"{tmo}</tptz:ContinuousMove>"
        )
        self.call(self._xaddr("PTZ"), body)

    def stop(self, profile: str) -> None:
        body = (
            f"<tptz:Stop><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>false</tptz:Zoom></tptz:Stop>"
        )
        self.call(self._xaddr("PTZ"), body)

    def relative_move(self, profile: str, translation: PtzVector) -> None:
        body = (
            f"<tptz:RelativeMove><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f'<tptz:Translation><tt:PanTilt x="{translation.pan}" y="{translation.tilt}"/></tptz:Translation>'
            "</tptz:RelativeMove>"
        )
        self.call(self._xaddr("PTZ"), body)

    def absolute_move(self, profile: str, position: PtzVector) -> None:
        body = (
            f"<tptz:AbsoluteMove><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
            f'<tptz:Position><tt:PanTilt x="{position.pan}" y="{position.tilt}"/></tptz:Position>'
            "</tptz:AbsoluteMove>"
        )
        self.call(self._xaddr("PTZ"), body)
