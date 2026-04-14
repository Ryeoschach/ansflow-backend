from rest_framework.renderers import JSONRenderer


class GlobalJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        if renderer_context:
            response = renderer_context['response']

            # 如果是 204 No Content，直接返回，不进行包装
            if response.status_code == 204:
                return super().render(data, accepted_media_type, renderer_context)

            # 如果 data 里包含 results 字段，说明它是经过自定义分页类包装的
            if isinstance(data, dict) and 'results' in data:
                # 把 results 里面的数据取出来给 data
                results = data.pop('results')
                res_data = {
                    'code': response.status_code,
                    'message': 'success' if response.status_code < 400 else 'error',
                    'data': results,
                    **data  # 把总条数、页码等其他分页元数据也平级放进去
                }
                return super().render(res_data, accepted_media_type, renderer_context)
            if isinstance(data, dict) and all(k in data for k in ('code', 'message', 'data')):
                return super().render(data, accepted_media_type, renderer_context)
            res_data = {
                'code': response.status_code,
                'message': 'success' if response.status_code < 400 else 'error',
                'data': data
            }
            return super().render(res_data, accepted_media_type, renderer_context)

        return super().render(data, accepted_media_type, renderer_context)