"""模拟网络搜索：在预置样例集合内做确定性的大小写无关子串匹配，不代表实时联网结果。"""

import json
from collections.abc import Callable
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


SEARCH_CORPUS: tuple[SearchResult, ...] = (
    {
        "title": "退货政策说明",
        "url": "https://example.com/policies/return",
        "snippet": "签收后七日内可申请退货，商品需保持外包装完整、配件齐全。",
    },
    {
        "title": "退款到账时间",
        "url": "https://example.com/policies/refund",
        "snippet": "退货验收通过后，退款按原支付渠道退回，银行卡到账时间以发卡行为准。",
    },
    {
        "title": "换货流程",
        "url": "https://example.com/policies/exchange",
        "snippet": "在订单详情页发起换货申请，选择新规格后由仓库核对库存并发货。",
    },
    {
        "title": "配送时效与运费",
        "url": "https://example.com/policies/shipping",
        "snippet": "现货商品下单后 48 小时内出库，偏远地区运费按订单金额另行计算。",
    },
    {
        "title": "物流异常处理",
        "url": "https://example.com/policies/logistics",
        "snippet": "物流信息超过 72 小时未更新时，可提交异常工单由客服核实包裹状态。",
    },
    {
        "title": "发票开具规则",
        "url": "https://example.com/policies/invoice",
        "snippet": "订单完成后可申请电子普通发票，发票抬头需与实名信息一致。",
    },
    {
        "title": "优惠券使用限制",
        "url": "https://example.com/policies/coupon",
        "snippet": "优惠券按面额抵扣，不可与同类满减活动叠加，过期后不予补发。",
    },
    {
        "title": "会员积分规则",
        "url": "https://example.com/policies/points",
        "snippet": "确认收货后按实付金额累计积分，积分可在下单时抵扣部分金额。",
    },
    {
        "title": "账户与隐私说明",
        "url": "https://example.com/policies/privacy",
        "snippet": "账户手机号用于登录与订单通知，未经授权不向第三方提供个人信息。",
    },
    {
        "title": "售后客服联系方式",
        "url": "https://example.com/policies/service",
        "snippet": "在线客服每日 9:00 至 21:00 受理咨询，工单会在 24 小时内回复。",
    },
)


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, description="搜索关键词")
    limit: int = Field(default=5, ge=1, le=10, description="返回结果条数，1 到 10")


def search(request: SearchQuery, *, start: Callable[[], bool] | None = None) -> str | None:
    if start is not None and not start():
        return None
    needle = request.query.strip().casefold()
    if not needle:
        raise ValueError("搜索关键词不能为空")
    results = [
        item
        for item in SEARCH_CORPUS
        if needle in item["title"].casefold() or needle in item["snippet"].casefold()
    ][: request.limit]
    return json.dumps({"source": "mock", "results": results}, ensure_ascii=False)
