import json
import os
import shutil
from collections import defaultdict
from hashlib import md5
from socket import gethostname

import yaml
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from assets.automations.methods import platform_automation_methods
from common.utils import get_logger, lazyproperty
from common.utils import ssh_pubkey_gen, ssh_key_string_to_obj
from ops.ansible import JMSInventory, PlaybookRunner, DefaultCallback

logger = get_logger(__name__)


class PlaybookCallback(DefaultCallback):
    def playbook_on_stats(self, event_data, **kwargs):
        super().playbook_on_stats(event_data, **kwargs)


class BasePlaybookManager:
    bulk_size = 100
    ansible_account_policy = 'privileged_first'
    ansible_account_prefer = 'root,Administrator'

    def __init__(self, execution):
        self.execution = execution
        self.method_id_meta_mapper = {
            method['id']: method
            for method in self.platform_automation_methods
            if method['method'] == self.__class__.method_type()
        }
        # 根据执行方式就行分组, 不同资产的改密、推送等操作可能会使用不同的执行方式
        # 然后根据执行方式分组, 再根据 bulk_size 分组, 生成不同的 playbook
        # 避免一个 playbook 中包含太多的主机
        self.method_hosts_mapper = defaultdict(list)
        self.playbooks = []

    @property
    def platform_automation_methods(self):
        return platform_automation_methods

    @classmethod
    def method_type(cls):
        raise NotImplementedError

    def get_assets_group_by_platform(self):
        return self.execution.all_assets_group_by_platform()

    @lazyproperty
    def runtime_dir(self):
        ansible_dir = settings.ANSIBLE_DIR
        task_name = self.execution.snapshot['name']
        dir_name = '{}_{}'.format(task_name.replace(' ', '_'), self.execution.id)
        path = os.path.join(
            ansible_dir, 'automations', self.execution.snapshot['type'],
            dir_name, timezone.now().strftime('%Y%m%d_%H%M%S')
        )
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True, mode=0o755)
        if settings.DEBUG_DEV:
            logger.debug('Ansible runtime dir: {}'.format(path))
        return path

    @staticmethod
    def write_cert_to_file(filename, content):
        with open(filename, 'w') as f:
            f.write(content)
        return filename

    def convert_cert_to_file(self, host, path_dir):
        if not path_dir:
            return host

        specific = host.get('jms_asset', {}).get('secret_info', {})
        cert_fields = ('ca_cert', 'client_key', 'client_cert')
        filtered = list(filter(lambda x: specific.get(x), cert_fields))
        if not filtered:
            return host

        cert_dir = os.path.join(path_dir, 'certs')
        if not os.path.exists(cert_dir):
            os.makedirs(cert_dir, 0o700, True)

        for f in filtered:
            result = self.write_cert_to_file(
                os.path.join(cert_dir, f), specific.get(f)
            )
            host['jms_asset']['secret_info'][f] = result
        return host

    def host_callback(self, host, automation=None, **kwargs):
        enabled_attr = '{}_enabled'.format(self.__class__.method_type())
        method_attr = '{}_method'.format(self.__class__.method_type())

        method_enabled = automation and \
                         getattr(automation, enabled_attr) and \
                         getattr(automation, method_attr) and \
                         getattr(automation, method_attr) in self.method_id_meta_mapper

        if not method_enabled:
            host['error'] = _('{} disabled'.format(self.__class__.method_type()))
            return host

        host = self.convert_cert_to_file(host, kwargs.get('path_dir'))
        return host

    @staticmethod
    def generate_public_key(private_key):
        return ssh_pubkey_gen(private_key=private_key, hostname=gethostname())

    @staticmethod
    def generate_private_key_path(secret, path_dir):
        key_name = '.' + md5(secret.encode('utf-8')).hexdigest()
        key_path = os.path.join(path_dir, key_name)

        if not os.path.exists(key_path):
            ssh_key_string_to_obj(secret, password=None).write_private_key_file(key_path)
            os.chmod(key_path, 0o400)
        return key_path

    def generate_inventory(self, platformed_assets, inventory_path):
        inventory = JMSInventory(
            assets=platformed_assets,
            account_prefer=self.ansible_account_prefer,
            account_policy=self.ansible_account_policy,
            host_callback=self.host_callback,
        )
        inventory.write_to_file(inventory_path)

    def generate_playbook(self, platformed_assets, platform, sub_playbook_dir):
        method_id = getattr(platform.automation, '{}_method'.format(self.__class__.method_type()))
        method = self.method_id_meta_mapper.get(method_id)
        if not method:
            logger.error("Method not found: {}".format(method_id))
            return method
        method_playbook_dir_path = method['dir']
        sub_playbook_path = os.path.join(sub_playbook_dir, 'project', 'main.yml')
        shutil.copytree(method_playbook_dir_path, os.path.dirname(sub_playbook_path))

        with open(sub_playbook_path, 'r') as f:
            plays = yaml.safe_load(f)
        for play in plays:
            play['hosts'] = 'all'

        with open(sub_playbook_path, 'w') as f:
            yaml.safe_dump(plays, f)
        return sub_playbook_path

    def get_runners(self):
        # TODO 临时打印一下 找一下打印不出日志的原因
        print('ansible runner: 任务开始执行')
        assets_group_by_platform = self.get_assets_group_by_platform()
        print('ansible runner: 获取资产分组', assets_group_by_platform)
        runners = []
        for platform, assets in assets_group_by_platform.items():
            assets_bulked = [assets[i:i + self.bulk_size] for i in range(0, len(assets), self.bulk_size)]

            for i, _assets in enumerate(assets_bulked, start=1):
                sub_dir = '{}_{}'.format(platform.name, i)
                playbook_dir = os.path.join(self.runtime_dir, sub_dir)
                inventory_path = os.path.join(self.runtime_dir, sub_dir, 'hosts.json')
                self.generate_inventory(_assets, inventory_path)
                playbook_path = self.generate_playbook(_assets, platform, playbook_dir)

                runer = PlaybookRunner(
                    inventory_path,
                    playbook_path,
                    self.runtime_dir,
                    callback=PlaybookCallback(),
                )
                runners.append(runer)
        return runners

    def on_host_success(self, host, result):
        pass

    def on_host_error(self, host, error, result):
        print('host error: {} -> {}'.format(host, error))

    def on_runner_success(self, runner, cb):
        summary = cb.summary
        for state, hosts in summary.items():
            for host in hosts:
                result = cb.host_results.get(host)
                if state == 'ok':
                    self.on_host_success(host, result)
                elif state == 'skipped':
                    # TODO
                    print('skipped: ', hosts)
                else:
                    error = hosts.get(host)
                    self.on_host_error(host, error, result)

    def on_runner_failed(self, runner, e):
        print("Runner failed: {} {}".format(e, self))

    def before_runner_start(self, runner):
        pass

    @staticmethod
    def delete_sensitive_data(path):
        if settings.DEBUG_DEV:
            return

        with open(path, 'r') as f:
            d = json.load(f)
        def delete_keys(d, keys_to_delete):
            """
            递归函数：删除嵌套字典中的指定键
            """
            if not isinstance(d, dict):
                return d
            keys = list(d.keys())
            for key in keys:
                if key in keys_to_delete:
                    del d[key]
                else:
                    delete_keys(d[key], keys_to_delete)
            return d
        d = delete_keys(d, ['secret', 'ansible_password'])
        with open(path, 'w') as f:
            json.dump(d, f)

    def run(self, *args, **kwargs):
        runners = self.get_runners()
        if len(runners) > 1:
            print("### 分批次执行开始任务, 总共 {}\n".format(len(runners)))
        elif len(runners) == 1:
            print(">>> 开始执行任务\n")
        else:
            print("### 没有需要执行的任务\n")
            return

        self.execution.date_start = timezone.now()
        for i, runner in enumerate(runners, start=1):
            if len(runners) > 1:
                print(">>> 开始执行第 {} 批任务".format(i))
            self.before_runner_start(runner)
            try:
                cb = runner.run(**kwargs)
                self.delete_sensitive_data(runner.inventory)
                self.on_runner_success(runner, cb)
            except Exception as e:
                self.on_runner_failed(runner, e)
            print('\n')
        self.execution.status = 'success'
        self.execution.date_finished = timezone.now()
        self.execution.save()
