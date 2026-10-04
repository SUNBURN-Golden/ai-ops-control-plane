"""Trusted, scoped config evidence. Never opens an authentication file."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from common import AppError, parse_json

ERROR='MAC_CODEX_CONFIG_CHANGED'

def require(value):
    if not value:raise AppError(ERROR)

def sha(value):return hashlib.sha256(value).hexdigest()

def without_trust(document,checkout):
    result=copy.deepcopy(document)
    projects=result.get('projects') or {}
    if checkout in projects:
        projects[checkout].pop('trust_level',None)
        if not projects[checkout]:projects.pop(checkout)
    if not projects:result.pop('projects',None)
    return result


class Snapshot:
    """Retain only target trust text; hash non-target bytes without parsing values."""
    def __init__(self,path,checkout):
        self.path=Path(path);self.checkout=str(Path(checkout).resolve())
        require(self.path.name=='config.toml')
        info=self.path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and
                not(info.st_mode & 0o022) and info.st_nlink==1 and info.st_size<=4194304)
        self.mode=stat.S_IMODE(info.st_mode)
        self.raw=self.path.read_bytes()
        # Conservative handling: quoted multiline TOML must not spoof headers.
        require(b'"""' not in self.raw and b"'''" not in self.raw)
        headers=(('[projects.'+json.dumps(self.checkout)+']').encode(),
                 ("[projects.'"+self.checkout+"']").encode())
        self.header=None;self.line=None;self.value=None;self.end=None
        offset=0;inside=False
        for text in self.raw.splitlines(keepends=True):
            stripped=text.strip()
            if stripped.startswith(b'['):
                if inside:self.end=offset;inside=False
                if any(stripped==header or stripped.startswith(header+b' #') for header in headers):
                    require(self.header is None)
                    self.header=(offset,offset+len(text));inside=True
            elif inside and re.match(rb'trust_level\s*=',stripped):
                require(self.line is None)
                match=re.fullmatch(rb'trust_level\s*=\s*([\'"])(trusted|untrusted)\1\s*(?:#[^\r\n]*)?',stripped)
                require(match is not None)
                self.line=(offset,offset+len(text));self.value=match[2].decode()
            offset+=len(text)
        if inside:self.end=len(self.raw)
        self.non_target_sha256=sha(self.masked())

    def masked(self):
        if self.line:return self.raw[:self.line[0]]+self.raw[self.line[1]:]
        return self.raw

    def journal(self):
        return {'config_file':str(self.path),'checkout':self.checkout,'mode':self.mode,
                'previous_project_table_present':self.header is not None,'previous_trust':self.value,
                'previous_trust_text':self.raw[slice(*self.line)].decode() if self.line else None,
                'non_target_sha256':self.non_target_sha256,'whole_config_sha256':sha(self.raw)}

    def unchanged(self):
        current=Snapshot(self.path,self.checkout)
        return current.raw==self.raw and current.mode==self.mode

    def verify(self,after):
        require(after.path==self.path and after.checkout==self.checkout and after.mode==self.mode and after.value=='trusted')
        if self.header is not None:
            require(after.header is not None and after.masked()==self.masked())
        else:
            require(after.raw.startswith(self.raw) and after.header and after.line)
            # A previously absent target may append only its header/trust line.
            delta=after.raw[len(self.raw):]
            require(after.header[0]>=len(self.raw) and after.end==len(after.raw))
            relative_header=(after.header[0]-len(self.raw),after.header[1]-len(self.raw))
            relative_line=(after.line[0]-len(self.raw),after.line[1]-len(self.raw))
            rest=delta[:relative_header[0]]+delta[relative_header[1]:relative_line[0]]+delta[relative_line[1]:]
            require(not rest.strip())
        return True


def user_layer(response):
    layers=[v for v in response.get('layers') or [] if v['name']['type']=='user' and v['name'].get('profile') is None]
    require(len(layers)==1)
    return layers[0]

def approved(root,checkout):
    path=Path(root)/'codex-trust-approval.json'
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and
                    not(info.st_mode & 0o022) and info.st_size<=65536)
            record=parse_json(stream.read().decode(),65536)
        require(record.get('schema_version')==1 and record.get('checkout')==str(Path(checkout).resolve()) and
                record.get('field')=='trust_level' and record.get('value')=='trusted' and record.get('user_approved') is True)
    except (OSError,ValueError,AppError):raise AppError('MAC_CODEX_TRUST_REQUIRED') from None
    return record
