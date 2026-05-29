from django.core.management.base import BaseCommand
from apps.host_management.models import ComplianceFramework, ComplianceClause

class Command(BaseCommand):
    help = "初始化等保 2.0 (网络安全等级保护 2.0 - 三级) 合规参考框架与标准条款数据"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("--- 开始初始化等保 2.0 参考数据 ---"))

        # 1. 创建合规框架
        framework, created = ComplianceFramework.objects.get_or_create(
            code="MLPS_2_0_L3",
            defaults={
                "name": "等保2.0参考",
                "version": "GB/T 22239-2019",
                "description": "网络安全等级保护 2.0 (等保三级) 安全计算环境对主机操作系统的安全加固合规对照参考"
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"创建框架: {framework.name}"))
        else:
            self.stdout.write(f"框架已存在: {framework.name}")

        # 2. 定义安全条款树
        clauses_data = [
            {
                "code": "S3.A1",
                "name": "身份鉴别 (Authentication)",
                "description": "应对登录的用户进行身份标识和鉴别，身份标识具有唯一性，身份鉴别信息具有复杂度要求并定期更换。",
                "sort_order": 1,
                "children": [
                    {
                        "code": "S3.A1.1",
                        "name": "口令复杂度与有效期",
                        "description": "检查口令复杂度限制（长度、字母大小写和特殊字符种类）与密码有效期设置（最大生存周期不超过 90 天）。",
                        "sort_order": 10
                    },
                    {
                        "code": "S3.A1.2",
                        "name": "登录失败锁定处理",
                        "description": "检查系统登录失败处理配置，当登录失败次数达到设定阈值后，系统自动采取账号或终端IP锁定封锁措施。",
                        "sort_order": 11
                    },
                    {
                        "code": "S3.A1.3",
                        "name": "远程管理加密传输",
                        "description": "检查在对服务器主机进行远程管理维护时，是否采用 SSH 或 TLS 等高安全加密密文协议传输，禁止未加密协议（如 Telnet）。",
                        "sort_order": 12
                    }
                ]
            },
            {
                "code": "S3.A2",
                "name": "访问控制 (Access Control)",
                "description": "应重命名或删除默认账户，修改默认账户密码；分配最小权限实现管理角色分离；设置操作超时退出限制。",
                "sort_order": 2,
                "children": [
                    {
                        "code": "S3.A2.1",
                        "name": "系统冗余与特权账号锁定",
                        "description": "检查并锁定非必需的系统预置或保留默认账号，清理无用账号，禁止超级管理员特权（UID=0）由除 root 以外的他人账户共享。",
                        "sort_order": 20
                    },
                    {
                        "code": "S3.A2.2",
                        "name": "管理角色最小权限分离",
                        "description": "核实系统管理特权的配置，实现管理员账号的最少权限分配和核心职责分离（如系统、安全、审计员的“三权分立”）。",
                        "sort_order": 21
                    },
                    {
                        "code": "S3.A2.3",
                        "name": "终端连接超时退出 (TMOUT)",
                        "description": "检查系统环境变量 TMOUT 设置，确保无操作超时自动断开的时限配置合理（建议在 600 秒以内）。",
                        "sort_order": 22
                    },
                    {
                        "code": "S3.A2.4",
                        "name": "远程访问源IP地址限制",
                        "description": "通过系统的 hosts.deny/allow 配置文件或本地包过滤防火墙策略，限制仅允许授信的管理终端远程访问管理端口。",
                        "sort_order": 23
                    }
                ]
            },
            {
                "code": "S3.A3",
                "name": "安全审计 (Security Audit)",
                "description": "应启用安全审计功能，记录覆盖到每个用户与重要安全事件，并妥善保存审计记录至少 6 个月。",
                "sort_order": 3,
                "children": [
                    {
                        "code": "S3.A3.1",
                        "name": "审计进程与日志服务运行",
                        "description": "检查 Linux 内置审计子系统服务进程 auditd 和系统日志守护进程 rsyslog 是否被设置为随系统启动并运行良好。",
                        "sort_order": 30
                    },
                    {
                        "code": "S3.A3.2",
                        "name": "安全审计策略与日志记录",
                        "description": "验证系统的安全审计策略是否有效捕捉到了所有用户的命令行活动、文件系统特权操作、SSH 登录尝试等核心审计事件。",
                        "sort_order": 31
                    },
                    {
                        "code": "S3.A3.3",
                        "name": "审计日志妥善保存六个月",
                        "description": "确保系统日志文件的转储轮转机制（logrotate）正常运行，确认归档的日志历史保存时长不少于 180 天。",
                        "sort_order": 32
                    }
                ]
            },
            {
                "code": "S3.A4",
                "name": "入侵防范 (Intrusion Prevention)",
                "description": "应关闭不必要的系统服务、默认共享和高危端口；应能发现或限制系统的漏洞，并及时进行安全补丁更新。",
                "sort_order": 4,
                "children": [
                    {
                        "code": "S3.A4.1",
                        "name": "禁用不必要的高危服务",
                        "description": "核实网络高危和敏感端口是否暴露，关闭并卸载不必要的外部共享服务、旧守护进程（如 FTP, sendmail, telnet ）。",
                        "sort_order": 40
                    },
                    {
                        "code": "S3.A4.2",
                        "name": "系统补丁与漏洞修复",
                        "description": "定期对操作系统核心、已安装工具包和系统服务进行 CVE 漏洞比对扫描，检查已应用的安全补丁以消减中高危漏洞风险。",
                        "sort_order": 41
                    }
                ]
            }
        ]

        def sync_clauses(clauses, parent=None):
            for data in clauses:
                children = data.pop("children", [])
                code = data["code"]
                
                clause, created_clause = ComplianceClause.objects.get_or_create(
                    framework=framework,
                    code=code,
                    defaults={
                        "parent": parent,
                        "name": data["name"],
                        "description": data.get("description", ""),
                        "sort_order": data.get("sort_order", 0)
                    }
                )
                
                if not created_clause:
                    clause.name = data["name"]
                    clause.description = data.get("description", "")
                    clause.parent = parent
                    clause.sort_order = data.get("sort_order", 0)
                    clause.save()

                status_str = "NEW" if created_clause else "UPDATE"
                self.stdout.write(f"  [{status_str}] 条款: {clause.code} - {clause.name}")

                if children:
                    sync_clauses(children, parent=clause)

        sync_clauses(clauses_data)
        self.stdout.write(self.style.SUCCESS("等保 2.0 合规数据初始化成功！"))
