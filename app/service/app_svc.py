import ast
import asyncio
import copy
import os
import traceback
from datetime import datetime, date
from importlib import import_module

import aiohttp_jinja2
import jinja2

from app.objects.c_plugin import Plugin
from app.utility.base_service import BaseService


class AppService(BaseService):

    def __init__(self, application, config):
        self.application = application
        self.config = config
        self.log = self.add_service('app_svc', self)
        self.loop = asyncio.get_event_loop()

    async def start_sniffer_untrusted_agents(self):
        """
        Cyclic function that repeatedly checks if there are agents to be marked as untrusted
        :return: None
        """
        next_check = self.config['untrusted_timer']
        try:
            while True:
                await asyncio.sleep(next_check + 1)
                trusted_agents = await self.get_service('data_svc').locate('agents', match=dict(trusted=1))
                next_check = self.config['untrusted_timer']
                for a in trusted_agents:
                    silence_time = (datetime.now() - a.last_trusted_seen).total_seconds()
                    if silence_time > (self.config['untrusted_timer'] + int(a.sleep_max)):
                        self.log.debug('Agent (%s) now untrusted. Last seen %s sec ago' % (a.paw, int(silence_time)))
                        a.trusted = 0
                    else:
                        trust_time_left = self.config['untrusted_timer'] - silence_time
                        if trust_time_left < next_check:
                            next_check = trust_time_left
                await asyncio.sleep(15)
        except Exception:
            traceback.print_exc()

    async def find_link(self, unique):
        """
        Locate a given link by its unique property
        :param unique:
        :return:
        """
        for op in await self._services.get('data_svc').locate('operations'):
            exists = next((link for link in op.chain if link.unique == unique), None)
            if exists:
                return exists

    async def run_scheduler(self):
        """
        Kick off all scheduled jobs, as their schedule determines
        :return:
        """
        while True:
            interval = 60
            for s in await self.get_service('data_svc').locate('schedules'):
                now = datetime.now().time()
                diff = datetime.combine(date.today(), now) - datetime.combine(date.today(), s.schedule)
                if interval > diff.total_seconds() > 0:
                    self.log.debug('Pulling %s off the scheduler' % s.name)
                    sop = copy.deepcopy(s.task)
                    sop.set_start_details()
                    await self._services.get('data_svc').store(sop)
                    asyncio.create_task(self.run_operation(sop))
            await asyncio.sleep(interval)

    async def resume_operations(self):
        """
        Resume all unfinished operations
        :return: None
        """
        await asyncio.sleep(10)
        for op in await self.get_service('data_svc').locate('operations', match=dict(finish=None)):
            self.loop.create_task(self.run_operation(op))

    async def start_c2(self, app):
        for c2 in await self.get_service('data_svc').locate('c2'):
            c2.start(app)

    async def run_operation(self, operation):
        try:
            self.log.debug('Starting operation: %s' % operation.name)
            planner = await self._get_planning_module(operation)
            for phase in operation.adversary.phases:
                await planner.execute(phase)
                await operation.wait_for_phase_completion()
                operation.phase = phase
            await self._cleanup_operation(operation)
            await operation.close()
            self.log.debug('Completed operation: %s' % operation.name)
        except Exception:
            traceback.print_exc()

    async def load_plugins(self):
        """
        Store all plugins in the data store
        :return:
        """
        for plug in os.listdir('plugins'):
            if not os.path.isdir('plugins/%s' % plug) or not os.path.isfile('plugins/%s/hook.py' % plug):
                self.log.error('Problem validating the "%s" plugin. Ensure CALDERA was cloned recursively.' % plug)
                exit(0)
            self.log.debug('Loading plugin: %s' % plug)
            plugin = Plugin(name=plug)
            await self.get_service('data_svc').store(plugin)
            if plugin.name in self.config['enabled_plugins']:
                plugin.enabled = True
        for plug in await self._services.get('data_svc').locate('plugins'):
            if plug.name in self.config['enabled_plugins'] or plug.enabled:
                await plug.enable(self.get_services())

        templates = ['plugins/%s/templates' % p.name.lower()
                     for p in await self.get_service('data_svc').locate('plugins')]
        aiohttp_jinja2.setup(self.application, loader=jinja2.FileSystemLoader(templates))

    """ PRIVATE """

    async def _get_planning_module(self, operation):
        planning_module = import_module(operation.planner.module)
        planner_params = ast.literal_eval(operation.planner.params)
        return getattr(planning_module, 'LogicalPlanner')(operation,
                                                          self.get_service('planning_svc'), **planner_params)

    async def _cleanup_operation(self, operation):
        for member in operation.agents:
            for link in await self.get_service('planning_svc').get_cleanup_links(operation, member):
                operation.add_link(link)
        await operation.wait_for_phase_completion()
