"""收藏按买家隔离读取与维护。"""
import json
from fastapi import HTTPException,Query,Request
from pydantic import BaseModel,ConfigDict,Field
from app.presentation.identity import require_buyer

class FavoriteWrite(BaseModel):
    model_config=ConfigDict(extra='forbid')
    product: dict=Field()

def register_favorite_routes(api,get_store):
    @api.get('/commerce/favorites')
    async def favorites(request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        return {'products':await get_store().list(buyer)}

    @api.put('/commerce/favorites/{product_id}')
    async def save_favorite(product_id:str,body:FavoriteWrite,request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        if body.product.get('product_id')!=product_id or not 1<=len(product_id)<=128 or len(json.dumps(body.product))>30000:
            raise HTTPException(422,'收藏商品信息无效')
        store=get_store()
        try:await store.save(buyer,body.product)
        except ValueError as error:raise HTTPException(422,str(error)) from error
        return {'products':await store.list(buyer)}

    @api.delete('/commerce/favorites/{product_id}')
    async def delete_favorite(product_id:str,request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id);store=get_store()
        await store.delete(buyer,product_id)
        return {'products':await store.list(buyer)}
