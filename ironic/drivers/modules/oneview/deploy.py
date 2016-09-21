# Copyright 2016 Hewlett Packard Enterprise Development LP.
# Copyright 2016 Universidade Federal de Campina Grande
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc

from futurist import periodics
from oslo_log import log as logging
import six

from ironic.common import exception
from ironic.common.i18n import _LE
from ironic.common.i18n import _LI
from ironic.common import states
from ironic.drivers.modules import agent
from ironic.drivers.modules import iscsi_deploy
from ironic.drivers.modules.oneview import common
from ironic.drivers.modules.oneview import deploy_utils
from ironic import objects

LOG = logging.getLogger(__name__)

CONF = common.CONF


@six.add_metaclass(abc.ABCMeta)
class OneViewPeriodicTasks(object):

    @abc.abstractproperty
    def oneview_driver(self):
        pass

    @periodics.periodic(spacing=CONF.oneview.periodic_check_interval,
                        enabled=CONF.oneview.enable_periodic_tasks)
    def _periodic_check_nodes_taken_by_oneview(self, manager, context):
        """Checks if nodes in Ironic were taken by OneView users.

        This driver periodic task will check for nodes that were taken by
        OneView users while the node is in available state, set the node to
        maintenance mode with an appropriate maintenance reason message and
        move the node to manageable state.

        :param manager: a ConductorManager instance
        :param context: request context
        :returns: None.
        """

        filters = {
            'provision_state': states.AVAILABLE,
            'maintenance': False,
            'driver': self.oneview_driver
        }
        node_iter = manager.iter_nodes(filters=filters)

        for node_uuid, driver in node_iter:

            node = objects.Node.get(context, node_uuid)

            try:
                oneview_using = deploy_utils.is_node_in_use_by_oneview(node)
            except exception.OneViewError as e:
                LOG.error(_LE("Error while determining if node "
                              "%(node_uuid)s is in use by OneView. "
                              "Error: %(error)s"),
                          {'node_uuid': node.uuid, 'error': e})

            if oneview_using:
                purpose = (_LI('Updating node %(node_uuid)s in use '
                               'by OneView from %(provision_state)s state '
                               'to %(target_state)s state and maintenance '
                               'mode %(maintenance)s.'),
                           {'node_uuid': node_uuid,
                            'provision_state': states.AVAILABLE,
                            'target_state': states.MANAGEABLE,
                            'maintenance': True})

                LOG.info(purpose)

                node.maintenance = True
                node.maintenance_reason = common.NODE_IN_USE_BY_ONEVIEW
                manager.update_node(context, node)
                manager.do_provisioning_action(context, node.uuid, 'manage')

    @periodics.periodic(spacing=CONF.oneview.periodic_check_interval,
                        enabled=CONF.oneview.enable_periodic_tasks)
    def _periodic_check_nodes_freed_by_oneview(self, manager, context):
        """Checks if nodes taken by OneView users were freed.

        This driver periodic task will be responsible to poll the nodes that
        are in maintenance mode and on manageable state to check if the Server
        Profile was removed, indicating that the node was freed by the OneView
        user. If so, it'll provide the node, that will pass through the
        cleaning process and become available to be provisioned.

        :param manager: a ConductorManager instance
        :param context: request context
        :returns: None.
        """

        filters = {
            'provision_state': states.MANAGEABLE,
            'maintenance': True,
            'driver': self.oneview_driver
        }
        node_iter = manager.iter_nodes(fields=['maintenance_reason'],
                                       filters=filters)
        for node_uuid, driver, maintenance_reason in node_iter:

            if maintenance_reason == common.NODE_IN_USE_BY_ONEVIEW:

                node = objects.Node.get(context, node_uuid)

                try:
                    oneview_using = deploy_utils.is_node_in_use_by_oneview(
                        node
                    )
                except exception.OneViewError as e:
                    LOG.error(_LE("Error while determining if node "
                                  "%(node_uuid)s is in use by OneView. "
                                  "Error: %(error)s"),
                              {'node_uuid': node.uuid, 'error': e})

                if not oneview_using:
                    purpose = (_LI('Bringing node %(node_uuid)s back from '
                                   'use by OneView from %(provision_state)s '
                                   'state to %(target_state)s state and '
                                   'maintenance mode %(maintenance)s.'),
                               {'node_uuid': node_uuid,
                                'provision_state': states.MANAGEABLE,
                                'target_state': states.AVAILABLE,
                                'maintenance': False})

                    LOG.info(purpose)

                    node.maintenance = False
                    node.maintenance_reason = None
                    manager.update_node(context, node)
                    manager.do_provisioning_action(
                        context, node.uuid, 'provide'
                    )

    @periodics.periodic(spacing=CONF.oneview.periodic_check_interval,
                        enabled=CONF.oneview.enable_periodic_tasks)
    def _periodic_check_nodes_taken_on_cleanfail(self, manager, context):
        """Checks failed deploys due to Oneview users taking Server Hardware.

        This last driver periodic task will take care of nodes that would be
        caught on a race condition between OneView and a deploy by Ironic. In
        such cases, the validation will fail, throwing the node on deploy fail
        and, afterwards on clean fail.

        This task will set the node to maintenance mode with a proper reason
        message and move it to manageable state, from where the second task
        can rescue the node as soon as the Server Profile is removed.

        :param manager: a ConductorManager instance
        :param context: request context
        :returns: None.
        """

        filters = {
            'provision_state': states.CLEANFAIL,
            'driver': self.oneview_driver
        }
        node_iter = manager.iter_nodes(fields=['driver_internal_info'],
                                       filters=filters)

        for node_uuid, driver, driver_internal_info in node_iter:

            node_oneview_error = driver_internal_info.get('oneview_error')
            if node_oneview_error == common.SERVER_HARDWARE_ALLOCATION_ERROR:

                node = objects.Node.get(context, node_uuid)

                purpose = (_LI('Bringing node %(node_uuid)s back from use '
                               'by OneView from %(provision_state)s state '
                               'to %(target_state)s state and '
                               'maintenance mode %(maintenance)s.'),
                           {'node_uuid': node_uuid,
                            'provision_state': states.CLEANFAIL,
                            'target_state': states.MANAGEABLE,
                            'maintenance': False})

                LOG.info(purpose)

                node.maintenance = True
                node.maintenance_reason = common.NODE_IN_USE_BY_ONEVIEW
                driver_internal_info = node.driver_internal_info
                driver_internal_info.pop('oneview_error', None)
                node.driver_internal_info = driver_internal_info
                manager.update_node(context, node)
                manager.do_provisioning_action(context, node.uuid, 'manage')


class OneViewIscsiDeploy(iscsi_deploy.ISCSIDeploy, OneViewPeriodicTasks):
    """Class for OneView ISCSI deployment driver."""

    oneview_driver = common.ISCSI_PXE_ONEVIEW

    def get_properties(self):
        deploy_utils.get_properties()

    def prepare(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.prepare(task)
        super(OneViewIscsiDeploy, self).prepare(task)

    def tear_down(self, task):
        if (common.is_dynamic_allocation_enabled(task.node) and
           not CONF.conductor.automated_clean):
            deploy_utils.tear_down(task)
        super(OneViewIscsiDeploy, self).tear_down(task)

    def prepare_cleaning(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.prepare_cleaning(task)
        return super(OneViewIscsiDeploy, self).prepare_cleaning(task)

    def tear_down_cleaning(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.tear_down_cleaning(task)
        return super(OneViewIscsiDeploy, self).tear_down_cleaning(task)


class OneViewAgentDeploy(agent.AgentDeploy, OneViewPeriodicTasks):
    """Class for OneView Agent deployment driver."""

    oneview_driver = common.AGENT_PXE_ONEVIEW

    def get_properties(self):
        deploy_utils.get_properties()

    def prepare(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.prepare(task)
        super(OneViewAgentDeploy, self).prepare(task)

    def tear_down(self, task):
        if (common.is_dynamic_allocation_enabled(task.node) and
           not CONF.conductor.automated_clean):
            deploy_utils.tear_down(task)
        super(OneViewAgentDeploy, self).tear_down(task)

    def prepare_cleaning(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.prepare_cleaning(task)
        return super(OneViewAgentDeploy, self).prepare_cleaning(task)

    def tear_down_cleaning(self, task):
        if common.is_dynamic_allocation_enabled(task.node):
            deploy_utils.tear_down_cleaning(task)
        return super(OneViewAgentDeploy, self).tear_down_cleaning(task)
