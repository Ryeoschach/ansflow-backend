from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

class MyCustomPagination(PageNumberPagination):
    # URL中用于指定页码的参数名
    page_query_param = 'page'

    # 支持两种参数名，兼容前端常见的 size 或 page_size
    page_size_query_param = 'page_size'

    # 默认的每页数量
    page_size = 10

    # 前端最多能设置的每页数量
    max_page_size = 1000

    def get_page_size(self, request):
        # 优先尝试从 ?size= 获取，如果没拿到，再尝试 ?page_size=
        # 老项目中有写死的，Todo: 处理老代码统一名称
        size = request.query_params.get('size') or request.query_params.get('page_size')
        if size:
            try:
                # 限制最大步长
                return min(int(size), self.max_page_size)
            except (TypeError, ValueError):
                pass
        return self.page_size

    def get_paginated_response(self, data):
        # 统一分页返回格式
        return Response({
            'total': self.page.paginator.count,  # 总条数
            'page': self.page.number,  # 当前页码
            'size': self.get_page_size(self.request),  # 当前每页大小
            'results': data  # 数据列表
        })