import subprocess
import json
import os
import tempfile
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from ..models import HelmRepository
from ..serializers import HelmRepositorySerializer
from utils.rbac_permission import SmartRBACPermission

class HelmRepositoryViewSet(viewsets.ModelViewSet):
    """
    Helm 仓库管理
    """
    queryset = HelmRepository.objects.all()
    serializer_class = HelmRepositorySerializer
    permission_classes = [SmartRBACPermission]
    resource_code = 'helm:repo'

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """
        测试仓库连通性
        """
        repo = self.get_object()
        temp_dir = tempfile.mkdtemp()
        try:
            # 模拟添加仓库
            cmd = ['helm', 'repo', 'add', repo.name, repo.url, '--repository-config', os.path.join(temp_dir, 'config.yaml'), '--repository-cache', temp_dir]
            if repo.username and repo.password:
                cmd.extend(['--username', repo.username, '--password', repo.password])
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return Response({"error": f"连接失败: {result.stderr or result.stdout}"}, status=status.HTTP_400_BAD_REQUEST)
            
            # 尝试更新
            subprocess.run(['helm', 'repo', 'update', repo.name, '--repository-config', os.path.join(temp_dir, 'config.yaml'), '--repository-cache', temp_dir], capture_output=True)
            
            return Response({"msg": "连接测试成功"})
        except Exception as e:
            return Response({"error": f"测试异常: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    @action(detail=True, methods=['get'])
    def charts_list(self, request, pk=None):
        """
        获取该仓库下的 Chart 列表
        """
        repo = self.get_object()
        temp_dir = tempfile.mkdtemp()
        try:
            config_p = os.path.join(temp_dir, 'config.yaml')
            # 1. Add
            add_cmd = ['helm', 'repo', 'add', repo.name, repo.url, '--repository-config', config_p, '--repository-cache', temp_dir]
            if repo.username and repo.password:
                add_cmd.extend(['--username', repo.username, '--password', repo.password])
            subprocess.run(add_cmd, capture_output=True, check=True)
            
            # 2. Search
            search_cmd = ['helm', 'search', 'repo', f'{repo.name}/', '--output', 'json', '--repository-config', config_p, '--repository-cache', temp_dir]
            res = subprocess.run(search_cmd, capture_output=True, text=True)
            
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return Response(data)
            return Response([])
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
