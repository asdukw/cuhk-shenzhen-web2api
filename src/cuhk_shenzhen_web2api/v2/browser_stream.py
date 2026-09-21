"""Bounded per-request browser NDJSON queues and explicit cancellation."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any

START = r"""({id,url,origin,payload,csrf,timeout}) => {
  if(location.origin !== origin || new URL(url).origin !== origin) return false;
  const streams = globalThis.__campusV2Streams ||= new Map();
  const s = {queue:[],closed:false,done:false,wake:null,controller:new AbortController(),reader:null};
  streams.set(id,s);
  const timer = setTimeout(()=>s.controller.abort(),timeout);
  const put = async item => {
    while(s.queue.length>=8 && !s.closed) await new Promise(r=>s.wake=r);
    if(s.closed) throw new Error('closed');
    s.queue.push(item);
  };
  s.task=(async()=>{
    try {
      const headers={'Content-Type':'application/json',Accept:'application/x-ndjson+json'};
      if(csrf) headers['X-CSRFToken']=csrf;
      const r=await fetch(url,{method:'POST',headers,body:JSON.stringify(payload),
        credentials:'same-origin',redirect:'manual',signal:s.controller.signal});
      await put({kind:'headers',status:r.status,ct:r.headers.get('content-type')||'',retry_after:r.headers.get('retry-after')});
      if(!r.ok || !r.body || !(r.headers.get('content-type')||'').includes('ndjson')) return;
      s.reader=r.body.getReader();
      const decoder=new TextDecoder('utf-8',{fatal:true});
      let pending='',total=0;
      const line=async text=>{
        if(new TextEncoder().encode(text).length>65536) throw new Error('limit');
        if(text.trim()) await put({kind:'line',text});
      };
      while(!s.closed){
        const {value,done}=await s.reader.read();
        if(done){pending+=decoder.decode();break;}
        total+=value.byteLength;
        if(total>2097152) throw new Error('limit');
        pending+=decoder.decode(value,{stream:true});
        let n;
        while((n=pending.indexOf('\n'))>=0){const text=pending.slice(0,n);pending=pending.slice(n+1);await line(text);}
        if(new TextEncoder().encode(pending).length>65536) throw new Error('limit');
      }
      if(pending.trim()) await line(pending);
    }catch(_){if(!s.closed)s.error='browser_stream_failed';}
    finally{clearTimeout(timer);s.done=true;if(s.reader){try{await s.reader.cancel();}catch(_){}}}
  })();
  return true;
}"""

TAKE = r"""id=>{
  const s=globalThis.__campusV2Streams?.get(id);
  if(!s)return {kind:'error',code:'stream_missing'};
  if(s.queue.length){const item=s.queue.shift();if(s.wake){const wake=s.wake;s.wake=null;wake();}return item;}
  if(s.done)return s.error?{kind:'error',code:s.error}:{kind:'eof'};
  return {kind:'wait'};
}"""

STOP = r"""async id=>{
  const streams=globalThis.__campusV2Streams,s=streams?.get(id);if(!s)return;
  s.closed=true;s.controller.abort();if(s.wake)s.wake();streams.delete(id);await s.task;
}"""


async def packets(page, args) -> AsyncGenerator[dict[str, Any], None]:
    request_id = uuid.uuid4().hex
    try:
        async with asyncio.timeout(args["timeout"] / 1000 + 2):
            if not await page.evaluate(START, {**args, "id": request_id}):
                yield {"kind": "error", "code": "wrong_browser_origin"}
                return
            while True:
                packet = await page.evaluate(TAKE, request_id)
                if packet["kind"] == "wait":
                    await asyncio.sleep(0.01)
                    continue
                yield packet
                if packet["kind"] in {"eof", "error"}:
                    return
    finally:

        async def cleanup():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.evaluate(STOP, request_id), 5)

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
