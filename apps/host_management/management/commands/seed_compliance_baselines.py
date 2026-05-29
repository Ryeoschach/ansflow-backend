from django.core.management.base import BaseCommand
from apps.host_management.models import ResourcePool, HostBaseline, ComplianceClause, ComplianceBaselineMapping

class Command(BaseCommand):
    help = "预置等保 2.0 (网络安全等级保护 2.0 - 三级) 经典巡检基线剧本，并自动关联对应合规条款"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("--- 开始创建等保 2.0 主机巡检基线 ---"))

        # 1. 查找或创建默认资源池
        pool = ResourcePool.objects.first()
        if not pool:
            pool = ResourcePool.objects.create(
                name="等保巡检主机池",
                code="mlps_inspection_pool",
                remark="专门用于执行等保合规基线巡检的主机集群池"
            )
            self.stdout.write(self.style.SUCCESS(f"创建默认合规资源池: {pool.name}"))
        else:
            self.stdout.write(f"使用现有资源池: {pool.name}")

        # 2. 定义基线数据
        baselines_data = [
            {
                "name": "【等保2.0】身份鉴别 - 密码复杂度与有效期",
                "clause_code": "S3.A1.1",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查密码最大使用天数
      shell: grep -E '^PASS_MAX_DAYS' /etc/login.defs | awk '{print $2}'
      register: pass_max_days
      changed_when: false

    - name: 检查密码复杂度配置是否启用
      shell: grep -E 'pam_pwquality.so|pam_cracklib.so' /etc/pam.d/system-auth
      register: pam_quality
      ignore_errors: yes
      changed_when: false

    - name: 判定合规性
      fail:
        msg: "密码最大周期超过 90 天，或未配置密码强度校验插件"
      when: >
        (pass_max_days.stdout | int > 90) or
        (pam_quality.rc != 0)""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 配置最大密码期为 90 天
      lineinfile:
        path: /etc/login.defs
        regexp: '^PASS_MAX_DAYS'
        line: 'PASS_MAX_DAYS   90'"""
            },
            {
                "name": "【等保2.0】身份鉴别 - 登录失败锁定处理",
                "clause_code": "S3.A1.2",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查登录锁定插件配置 (pam_faillock/pam_tally2)
      shell: grep -E 'pam_faillock.so|pam_tally2.so' /etc/pam.d/system-auth
      register: pam_lock
      ignore_errors: yes
      changed_when: false

    - name: 判定合规性
      fail:
        msg: "系统未配置登录失败锁定限制策略"
      when: pam_lock.rc != 0""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 注入登录失败锁定保护 (pam_faillock)
      lineinfile:
        path: /etc/pam.d/system-auth
        insertafter: 'auth        required      pam_env.so'
        line: 'auth        required      pam_faillock.so preauth silent audit deny=5 unlock_time=900'"""
            },
            {
                "name": "【等保2.0】身份鉴别 - 远程管理加密传输",
                "clause_code": "S3.A1.3",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 探测是否暴露出 Telnet 明文登录端口 (23)
      shell: ss -lntp | grep -E ':23\\b'
      register: telnet_port
      ignore_errors: yes
      changed_when: false

    - name: 检查 SSH 服务状态
      systemd:
        name: sshd
      register: ssh_status

    - name: 判定合规性
      fail:
        msg: "明文协议 Telnet 运行中，或安全 SSH 服务未启用"
      when: >
        (telnet_port.rc == 0) or
        (ssh_status.status.ActiveState != "active")""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 禁用并安全下线 Telnet 服务
      systemd:
        name: telnet.socket
        state: stopped
        enabled: no
      ignore_errors: yes

    - name: 开启并使能 SSHD 远程访问
      systemd:
        name: sshd
        state: started
        enabled: yes"""
            },
            {
                "name": "【等保2.0】访问控制 - 超级用户特权控制",
                "clause_code": "S3.A2.1",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查除 root 外是否存在 UID 为 0 的多余账户
      shell: awk -F: '($3 == 0 && $1 != "root") {print $1}' /etc/passwd
      register: root_accounts
      changed_when: false

    - name: 判定合规性
      fail:
        msg: "系统存在非授权特权用户: {{ root_accounts.stdout }}"
      when: root_accounts.stdout != "" """,
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 提示管理员手工核查
      debug:
        msg: "为保证系统安全，该修复涉及高危操作，请管理员手工限制或剔除该账号。" """
            },
            {
                "name": "【等保2.0】访问控制 - 终端会话超时退出",
                "clause_code": "S3.A2.3",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查超时断开环境变量 TMOUT 配置
      shell: "grep -E '^export TMOUT=' /etc/profile || grep -E '^TMOUT=' /etc/profile"
      register: tmout_grep
      ignore_errors: yes
      changed_when: false

    - name: 判定合规性 (TMOUT 必须 <= 600 秒)
      shell: "source /etc/profile && echo $TMOUT"
      register: tmout_val
      ignore_errors: yes
      changed_when: false

    - name: 评估
      fail:
        msg: "未配置 TMOUT 或数值大于 600 秒限制"
      when: >
        (tmout_grep.rc != 0) or
        (tmout_val.stdout == "" or tmout_val.stdout | int > 600)""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 将无操作会话超时时间限制为 600 秒
      lineinfile:
        path: /etc/profile
        regexp: '^export TMOUT='
        line: 'export TMOUT=600'"""
            },
            {
                "name": "【等保2.0】安全审计 - 审计服务与日志服务状态",
                "clause_code": "S3.A3.1",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查 auditd 审计服务是否在运行
      systemd:
        name: auditd
      register: auditd_status

    - name: 检查 rsyslog 系统日志是否在运行
      systemd:
        name: rsyslog
      register: rsyslog_status

    - name: 判定合规性
      fail:
        msg: "系统审计守护进程或日志服务异常未开启"
      when: >
        (auditd_status.status.ActiveState != "active") or
        (rsyslog_status.status.ActiveState != "active")""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 启动并设置 auditd 自动随系统挂载
      systemd:
        name: auditd
        state: started
        enabled: yes
      ignore_errors: yes

    - name: 启动 rsyslog 服务
      systemd:
        name: rsyslog
        state: started
        enabled: yes"""
            },
            {
                "name": "【等保2.0】安全审计 - 审计日志保存周期校验",
                "clause_code": "S3.A3.3",
                "check_playbook": """---
- hosts: all
  gather_facts: no
  tasks:
    - name: 检查 syslog 轮转保留天数配置 (是否配置 rotate 26)
      shell: "grep -E 'rotate 26|rotate 30|rotate 6' /etc/logrotate.d/syslog"
      register: logrotate_check
      ignore_errors: yes
      changed_when: false

    - name: 判定合规性
      fail:
        msg: "系统安全日志备份保留周期未达 6 个月"
      when: logrotate_check.rc != 0""",
                "remediate_playbook": """---
- hosts: all
  become: yes
  tasks:
    - name: 配置 logrotate 以保留 syslog 不低于 26 周 (约6个月)
      lineinfile:
        path: /etc/logrotate.d/syslog
        regexp: 'rotate\\\\s+\\\\d+'
        line: '    rotate 26'
      ignore_errors: yes"""
            }
        ]

        for b in baselines_data:
            # 1. 查找对应的合规条款
            clause = ComplianceClause.objects.filter(code=b["clause_code"]).first()
            if not clause:
                self.stdout.write(self.style.ERROR(f"找不到条款 {b['clause_code']}，跳过创建 {b['name']}"))
                continue

            # 2. 查找或创建基线
            baseline, created = HostBaseline.objects.get_or_create(
                name=b["name"],
                defaults={
                    "resource_pool": pool,
                    "check_playbook": b["check_playbook"],
                    "remediate_playbook": b["remediate_playbook"],
                    "auto_remediate": False,
                    "is_active": True
                }
            )

            # 如果已存在，更新其 Playbook 内容
            if not created:
                baseline.check_playbook = b["check_playbook"]
                baseline.remediate_playbook = b["remediate_playbook"]
                baseline.resource_pool = pool
                baseline.save()

            status_str = "NEW" if created else "UPDATE"
            self.stdout.write(f"  [{status_str}] 基线: {baseline.name}")

            # 3. 创建映射关系
            mapping, mapped_created = ComplianceBaselineMapping.objects.get_or_create(
                clause=clause,
                baseline=baseline
            )
            if mapped_created:
                self.stdout.write(self.style.SUCCESS(f"    [MAPPED] 映射到等保条款: {clause.code}"))

        self.stdout.write(self.style.SUCCESS("等保 2.0 主机巡检基线及映射初始化成功！"))
