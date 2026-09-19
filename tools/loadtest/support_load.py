#!/usr/bin/env python3
"""Synthetic API/WebSocket capacity test. Run from a different machine than the target VPS."""
import argparse
import asyncio
import statistics
import time
from urllib.parse import urlparse

import httpx
import websockets


def ws_url(base, sid):
    parsed=urlparse(base)
    scheme='wss' if parsed.scheme=='https' else 'ws'
    return f"{scheme}://{parsed.netloc}/api/ws/{sid}"


def cookie_header(client):
    return '; '.join(f"{c.name}={c.value}" for c in client.cookies.jar)


async def open_socket(base, sid, client):
    return await websockets.connect(
        ws_url(base,sid),
        origin=base.rstrip('/'),
        additional_headers={'Cookie':cookie_header(client)},
        max_size=65536,
        open_timeout=15,
    )


async def prepare_one(index, base, agent):
    started=time.perf_counter()
    inv=await agent.post('/api/invitations',json={
        'name':f'Load Test {index}',
        'description':'Teste sintético de capacidade',
        'phone':'',
        'company':'Load Test',
    })
    inv.raise_for_status()
    data=inv.json()
    token=data['url'].split('#',1)[1]
    client=httpx.AsyncClient(base_url=base,timeout=20,headers={'Origin':base.rstrip('/')})
    joined=await client.post('/api/join',json={'token':token,'code':''})
    joined.raise_for_status()
    sid=joined.json()['id']
    agent_ws,client_ws=await asyncio.gather(open_socket(base,sid,agent),open_socket(base,sid,client))
    await agent_ws.recv();await client_ws.recv()
    elapsed=time.perf_counter()-started
    return {'id':sid,'client':client,'agent_ws':agent_ws,'client_ws':client_ws,'open_seconds':elapsed}


async def keepalive(item,seconds):
    stop=time.monotonic()+seconds
    while time.monotonic()<stop:
        await asyncio.gather(
            item['agent_ws'].send('{"type":"ping"}'),
            item['client_ws'].send('{"type":"ping"}'),
        )
        await asyncio.sleep(10)


async def close_one(item,agent):
    try:
        await agent.post(f"/api/sessions/{item['id']}/end")
    except Exception:
        pass
    for ws in (item['agent_ws'],item['client_ws']):
        try: await ws.close()
        except Exception: pass
    await item['client'].aclose()


async def main(args):
    base=args.base_url.rstrip('/')
    agent=httpx.AsyncClient(base_url=base,timeout=30,headers={'Origin':base})
    login=await agent.post('/api/login',json={'email':args.email,'password':args.password})
    login.raise_for_status()

    print(f"Abrindo {args.sessions} atendimentos sintéticos...")
    results=await asyncio.gather(*(prepare_one(i+1,base,agent) for i in range(args.sessions)),return_exceptions=True)
    ok=[x for x in results if isinstance(x,dict)]
    errors=[x for x in results if not isinstance(x,dict)]
    if ok:
        times=[x['open_seconds'] for x in ok]
        times_sorted=sorted(times)
        p95=times_sorted[min(len(times_sorted)-1,max(0,int(len(times_sorted)*.95)-1))]
        print(f"abertos={len(ok)} erros={len(errors)} media={statistics.mean(times):.2f}s p95={p95:.2f}s max={max(times):.2f}s")
    if errors:
        for error in errors[:10]: print("ERRO:",repr(error))
        if any('429' in repr(e) for e in errors):
            print("Dica: /api/join aceita 30 entradas por hora por IP; rode a próxima rodada em outra hora ou de outra máquina.")

    if ok:
        print(f"Mantendo conexões por {args.hold}s. Observe Grafana agora.")
        await asyncio.gather(*(keepalive(item,args.hold) for item in ok),return_exceptions=True)
        await asyncio.gather(*(close_one(item,agent) for item in ok))
    await agent.aclose()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base-url',required=True)
    p.add_argument('--email',required=True)
    p.add_argument('--password',required=True)
    p.add_argument('--sessions',type=int,default=10,choices=range(1,101),metavar='1..100')
    p.add_argument('--hold',type=int,default=120)
    asyncio.run(main(p.parse_args()))
