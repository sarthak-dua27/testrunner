import copy
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import random
from threading import Thread

import global_vars

from SystemEventLogLib.Events import EventHelper
from SystemEventLogLib.gsi_events import IndexingServiceEvents
from failover.AutoFailoverBaseTest import AutoFailoverBaseTest
from couchbase_helper.documentgenerator import SDKDataLoader

from .base_gsi import BaseSecondaryIndexingTests
from couchbase_helper.query_definitions import QueryDefinition
from membase.helper.bucket_helper import BucketOperationHelper
from newupgradebasetest import NewUpgradeBaseTest
from remote.remote_util import RemoteMachineShellConnection
from membase.api.rest_client import RestConnection, RestHelper

log = logging.getLogger(__name__)
QUERY_TEMPLATE = "SELECT {0} FROM %s "


class UpgradeSecondaryIndex(BaseSecondaryIndexingTests, NewUpgradeBaseTest, AutoFailoverBaseTest):
    def setUp(self):
        super(UpgradeSecondaryIndex, self).setUp()
        self.initial_build_type = self.input.param('initial_build_type', None)
        self.upgrade_build_type = self.input.param('upgrade_build_type', self.initial_build_type)
        self.disable_plasma_upgrade = self.input.param("disable_plasma_upgrade", False)
        self.rebalance_empty_node = self.input.param("rebalance_empty_node", True)
        self.num_plasma_buckets = self.input.param("standard_buckets", 1)
        self.initial_version = self.input.param('initial_version', '4.6.0-3653')
        self.post_upgrade_gsi_type = self.input.param('post_upgrade_gsi_type', 'memory_optimized')
        self.upgrade_to = self.input.param("upgrade_to")
        self.index_batch_size = self.input.param("index_batch_size", -1)
        self.toggle_disable_upgrade = self.input.param("toggle_disable_upgrade", False)
        query_template = QUERY_TEMPLATE
        query_template = query_template.format("job_title")
        self.whereCondition = self.input.param("whereCondition", " job_title != \"Sales\" ")
        query_template += " WHERE {0}".format(self.whereCondition)
        self.load_query_definitions = []
        self.initial_index_number = self.input.param("initial_index_number", 1)
        self.run_mixed_mode_tests = self.input.param("run_mixed_mode_tests", False)
        if self.enable_dgm:
            if self.gsi_type == 'memory_optimized':
                self.skipTest("DGM can be achieved only for plasma")
            dgm_server = self.get_nodes_from_services_map(service_type="index")
            self.get_dgm_for_plasma(indexer_nodes=[dgm_server])
        for x in range(self.initial_index_number):
            index_name = "index_name_" + str(x)
            query_definition = QueryDefinition(index_name=index_name, index_fields=["job_title"],
                                               query_template=query_template, groups=["simple"])
            self.load_query_definitions.append(query_definition)
        if not self.build_index_after_create:
            self.build_index_after_create = True
            self.multi_create_index(buckets=self.buckets,
                                    query_definitions=self.load_query_definitions)
            self.build_index_after_create = False
        else:
            self.multi_create_index(buckets=self.buckets,
                                    query_definitions=self.load_query_definitions)
        self.skip_metabucket_check = True
        if self.enable_dgm:
            self.assertTrue(self._is_dgm_reached())

    def tearDown(self):
        self.upgrade_servers = self.servers
        super(UpgradeSecondaryIndex, self).tearDown()

    def test_offline_upgrade(self):
        """
        Offline Upgrade.
        1) Perform Operations
        2) Stop cb service on all nodes.
        3) Upgrade all nodes.
        4) Start cb service on all nodes.
        5) Perform Operations
        """

        # Perform pre_upgrade operations on cluster
        before_tasks = self.async_run_operations(buckets=self.buckets,
                                                 phase="before")
        self._run_tasks([before_tasks])
        prepare_statements = self._create_prepare_statement()
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        index_rest = RestConnection(index_node)
        pre_upgrade_index_stats = index_rest.get_all_index_stats()
        for server in self.servers:
            remote = RemoteMachineShellConnection(server)
            remote.stop_server()
            remote.disconnect()
            self.upgrade_servers.append(server)
        upgrade_threads = self._async_update(self.upgrade_to, self.servers)
        for upgrade_thread in upgrade_threads:
            upgrade_thread.join()
        self.add_built_in_server_user()
        ops_map = self.generate_operation_map("before")
        if "create_index" in ops_map and not self.build_index_after_create:
            index_name_list = []
            for query_definition in self.query_definitions:
                index_name_list.append(query_definition.index_name)
            build_index_tasks = []
            for bucket in self.buckets:
                build_index_tasks.append(self.async_build_index(
                    bucket, index_name_list))
            self._run_tasks([build_index_tasks])
        self.sleep(20)
        kv_ops = self.kv_mutations()
        for kv_op in kv_ops:
            kv_op.result()
        nodes = self.get_nodes_from_services_map(service_type="index",
                                                 get_all_nodes=True)
        for node in nodes:
            self._verify_indexer_storage_mode(node)
        self.multi_query_using_index(buckets=self.buckets, query_definitions=self.load_query_definitions)
        try:
            self._execute_prepare_statement(prepare_statements)
        except Exception as ex:
            msg = "No such prepared statement"
            self.assertIn(msg, str(ex), str(ex))
        self._verify_index_partitioning()
        post_upgrade_index_stats = index_rest.get_all_index_stats()

        # self.log.info(f"PRE:{pre_upgrade_index_stats}")
        # self.log.info(f"PRE:{post_upgrade_index_stats}")
        self._post_upgrade_task(task='stats_comparison', stats_before_upgrade=pre_upgrade_index_stats,
                                stats_after_upgrade=post_upgrade_index_stats)
        self._post_upgrade_task(task='create_collection')
        if self.num_index_replicas > 0:
            index_nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
            if len(index_nodes) > 1:
                self._post_upgrade_task(task='auto_failover')
            else:
                self.log.info("Can't run Auto-Failover tests for one Index node")
        self._post_upgrade_task(task='create_indexes')
        self._post_upgrade_task(task='run_query')
        self._post_upgrade_task(task='request_plus_scans')
        if self.enable_dgm:
            self.assertTrue(self._is_dgm_reached())
        self._post_upgrade_task(task='rebalance_in', node=self.servers[self.nodes_init])
        self._post_upgrade_task(task='rebalance_out', node=self.servers[self.nodes_init])
        self._post_upgrade_task(task='drop_all_indexes')
        # creating indexes again to check plasma sharding
        self._post_upgrade_task(task='create_indexes')

        # Neo Features
        self._post_upgrade_task(task='smart_batching')
        self._post_upgrade_task(task='system_event')
        self._post_upgrade_task(task='free_tier')


    def _mixed_mode_tasks(self):
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        if index_node:
            self.index_rest = RestConnection(index_node)

        # Checking if System events are getting logged
        system_event_api = f'{self.index_rest.baseUrl}/events'
        status, content, header = self.index_rest.urllib_request(api=system_event_api)
        err_msg_1 = "This http API endpoint isn't supported in mixed version clusters"
        err_msg_2 = "Not found."
        if err_msg_1 in str(content) or err_msg_2 in str(content):
            pass
        else:
            self.fail(f"Failed while retrieving System Events logs. {content}")

        # Checking smart-batching
        add_nodes = self.servers[self.nodes_init:]
        services = ['index'] * len(add_nodes)
        rebalance_task = self.cluster.async_rebalance(servers=self.servers[:self.nodes_init], to_add=add_nodes,
                                                      to_remove=[], services=services)
        self.validate_smart_batching_during_rebalance(rebalance_task)

    def _post_upgrade_task(self, task, num_replica=0, stats_before_upgrade=None, stats_after_upgrade=None,
                           node=None):
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        self.index_rest = RestConnection(index_node)
        system_query = 'select * from system:indexes;'
        if task == 'create_collection':
            self.prepare_collection_for_indexing(bucket_name=self.buckets[0].name, num_scopes=3, num_collections=3,
                                                 num_of_docs_per_collection=1000)
        elif task == 'create_indexes':
            result = self.index_rest.get_indexer_metadata()
            if 'status' in result:
                indexer_metadata = result['status']
            else:
                indexer_metadata = {}

            initial_index_count = len(indexer_metadata)
            err_msg1 = 'The index is scheduled for background creation'
            err_msg2 = 'Index creation will be retried in background'
            err_msg3 = 'will retry building in the background for reason: Build Already In Progress.'
            new_index_count = 0
            for namespace in self.namespaces:
                index_gen1 = QueryDefinition(index_name='idx1', index_fields=['age'])
                index_gen2 = QueryDefinition(index_name='idx2', index_fields=['city'])
                index_gen3 = QueryDefinition(index_name='idx3', index_fields=['country'])
                build_option = [True, False]
                defer_build = random.choice(build_option)
                try:
                    query1 = index_gen1.generate_index_create_query(namespace=namespace, num_replica=num_replica,
                                                                    defer_build=defer_build)
                    self.run_cbq_query(server=self.n1ql_node, query=query1)
                    if defer_build:
                        defer_query = index_gen1.generate_build_query(namespace=namespace)
                        self.run_cbq_query(server=self.n1ql_node, query=defer_query)
                except Exception as err:
                    if err_msg1 in str(err) or err_msg2 in str(err) or err_msg3 in str(err):
                        self.log.info(err)
                    else:
                        self.fail(err)

                try:
                    defer_build = random.choice(build_option)
                    query2 = index_gen2.generate_index_create_query(namespace=namespace, num_replica=num_replica,
                                                                    defer_build=defer_build)
                    self.run_cbq_query(server=self.n1ql_node, query=query2)
                    if defer_build:
                        defer_query = index_gen2.generate_build_query(namespace=namespace)
                        self.run_cbq_query(server=self.n1ql_node, query=defer_query)
                except Exception as err:
                    if err_msg1 in str(err) or err_msg2 in str(err) or err_msg3 in str(err):
                        self.log.info(err)
                    else:
                        self.fail(err)

                try:
                    defer_build = random.choice(build_option)
                    query3 = index_gen3.generate_index_create_query(namespace=namespace, num_replica=num_replica,
                                                                    defer_build=defer_build)
                    self.run_cbq_query(server=self.n1ql_node, query=query3)
                    if defer_build:
                        defer_query = index_gen3.generate_build_query(namespace=namespace)
                        self.run_cbq_query(server=self.n1ql_node, query=defer_query)
                except Exception as err:
                    if err_msg1 in str(err) or err_msg2 in str(err) or err_msg3 in str(err):
                        self.log.info(err)
                    else:
                        self.fail(err)
                new_index_count += 3
            self.wait_until_indexes_online()
            indexer_metadata = self.index_rest.get_indexer_metadata()['status']
            final_indexes = []
            for index in indexer_metadata:
                final_indexes.append(index['indexName'])
            try:
                self.assertEqual(initial_index_count + new_index_count, len(indexer_metadata))
            except Exception as err:
                self.log.error(err)

        elif task == 'run_query':
            try:
                for namespace in self.namespaces:
                    query1 = f'select count(age) from {namespace} where age > 10'
                    result = self.run_cbq_query(server=self.n1ql_node, query=query1)['results'][0]['$1']
                    self.assertTrue(result > 0)
                    query1 = f'select count(city) from {namespace} where city like "A%"'
                    result = self.run_cbq_query(server=self.n1ql_node, query=query1)['results'][0]['$1']
                    self.assertTrue(result > 0)
                    query1 = f'select count(age) from {namespace} where country like "A%"'
                    result = self.run_cbq_query(server=self.n1ql_node, query=query1)['results'][0]['$1']
                    self.assertTrue(result > 0)
            except Exception as err:
                self.log.error(err)

        elif task == 'stats_comparison':
            if not (stats_after_upgrade and stats_before_upgrade):
                self.fail("Provide PRE and POST upgrade stats for comparison")

            pre_upgrade_keys = set(stats_before_upgrade.keys())
            post_upgrade_keys = set(stats_after_upgrade.keys())
            bucket_stats = ["mutation_queue_size", "num_mutations_queued", "num_nonalign_ts",
                            "num_rollbacks", "timings/dcp_getseqs", "ts_queue_size"]
            new_post_upgrade_keys = set()
            for key in post_upgrade_keys:
                chunks = key.split(":")
                if chunks[-1] in bucket_stats and "MAINT_STREAM" in chunks[0]:
                    new_post_upgrade_keys.add(":".join(key.split(':')[1:]))
                else:
                    new_post_upgrade_keys.add(key)
            diff = pre_upgrade_keys - new_post_upgrade_keys
            if diff:
                self.log.error("Following stats keys are missing")
                self.log.error(diff)

        elif task == 'rebalance_in':
            if not node:
                self.fail("Node info not provided for Rebalancing In new node")
            node_rest = RestConnection(node)
            cb_version = "-".join(node_rest.get_nodes_version().split('-')[0:-1])
            if cb_version != self.upgrade_versions:
                upgrade_th = self._async_update(self.upgrade_to, [node])
                for th in upgrade_th:
                    th.join()
                self.sleep(120)
            self.log.info("Rebalance-In a new Indexer node for auto re-distribution of Indexes")
            redistribute = {"indexer.settings.rebalance.redistribute_indexes": True}
            self.index_rest.set_index_settings(redistribute)
            add_nodes = [node]
            task = self.cluster.async_rebalance(servers=self.servers[:self.nodes_init], to_add=add_nodes,
                                                to_remove=[], services=['index'])
            result = task.result()
            rebalance_status = RestHelper(self.rest).rebalance_reached()
            self.assertTrue(rebalance_status, "rebalance failed, stuck or did not complete")
            indexer_metadata = self.index_rest.get_indexer_metadata()['status']
            indexes_hosts = set()
            for index in indexer_metadata:
                for host in index['hosts']:
                    indexes_hosts.add(host.split(':')[0])
            self.assertTrue(self.servers[4].ip in indexes_hosts, "Indexes re-distribution failed for new Indexer Node")

        elif task == 'rebalance_out':
            if not node:
                self.fail("Node info not provided for Rebalance-Out a node")
            self.log.info("Rebalance-Out an Indexer node")
            remove_nodes = [node]
            task = self.cluster.async_rebalance(servers=self.servers[:self.nodes_init], to_add=[],
                                                to_remove=remove_nodes, services=['index'])
            task.result()
            rebalance_status = RestHelper(self.rest).rebalance_reached()
            self.assertTrue(rebalance_status, "rebalance failed, stuck or did not complete")

        elif task == 'drop_all_indexes':
            self.log.info("Dropping all indexes")
            result = self.run_cbq_query(server=self.n1ql_node, query=system_query)['results']
            for index_dict in result:
                index = index_dict['indexes']
                if 'scope_id' in index:
                    keyspacename = f'{index["bucket_id"]}.{index["scope_id"]}.{index["keyspace_id"]}'
                else:
                    keyspacename = index["keyspace_id"]
                index_name = index['name']
                query = f"Drop Index `{index_name}` ON {keyspacename}"
                self.run_cbq_query(server=self.n1ql_node, query=query)

            self.sleep(10, "Waiting before checking for Index traces")
            result = self.run_cbq_query(server=self.n1ql_node, query=system_query)['results']
            self.assertTrue(len(result) == 0, f"Not all indexes has been dropped. System query result: {result}")

        elif task == 'request_plus_scans':
            self.log.info("Running query to check index count")
            index_count_dict = {}
            for namespace in self.namespaces:
                count_query = f"Select count(*) from {namespace}"
                result = self.run_cbq_query(server=self.n1ql_node, query=count_query)['results'][0]['$1']
                index_count_dict[namespace] = result

            self.log.info("Adding new docs and running request plus scans")
            tasks = []
            num_ops = 10 ** 3
            with ThreadPoolExecutor() as executor:
                self.log.info("Loading new docs to collection")
                for namespace in self.namespaces:
                    _, keyspace = namespace.split(':')
                    bucket, scope, collection = keyspace.split('.')
                    gen_create = SDKDataLoader(num_ops=num_ops, percent_create=100,
                                               percent_update=0, percent_delete=0, scope=scope,
                                               collection=collection, key_prefix='request_plus_docs')
                    task = executor.submit(self._load_all_buckets, self.master, gen_create)
                    tasks.append(task)

                self.log.info("Running request plus scan till indexes index all the newly added docs")
                for namespace in self.namespaces:
                    count = 0
                    while count < 30:
                        count_query = f"Select count(*) from {namespace}"
                        result = self.run_cbq_query(server=self.n1ql_node, query=count_query,
                                                    scan_consistency='request_plus')['results'][0]['$1']
                        if result == (index_count_dict[namespace] + num_ops):
                            self.log.info(f"Select count result for request_plus scan:{result}")
                            break

                        self.sleep(10, f"Query result is not matching the expected value. Actual: {result}, Expected:"
                                       f"{index_count_dict[namespace] + num_ops}")
                        count += 1
                    else:
                        index_stats = self.index_rest.get_all_index_stats(text=True)
                        self.log.info(f"Index Stats: {index_stats}")
                        self.fail("Indexer not able to index all docs.")
        elif task == 'system_event':
            global_vars.system_event_logs = EventHelper()
            self.system_events = global_vars.system_event_logs
            self.system_events.set_test_start_time()
            index_name = 'sys_event_idx'
            index_gen = QueryDefinition(index_name=index_name, index_fields=['age'])
            query = index_gen.generate_index_create_query(namespace=self.namespaces[0],
                                                          defer_build=self.defer_build,
                                                          num_replica=self.num_index_replicas)
            self.run_cbq_query(server=self.n1ql_node, query=query)
            indexer_metadata = self.index_rest.get_indexer_metadata()['status']
            nodes_uuids = self.get_nodes_uuids()
            for index in indexer_metadata:
                if index['name'] == index_name:
                    instance_id = index['instId']
                    definition_id = index['defnId']
                    replica_id = index['replicaId']
                    node = index['hosts'][0].split(':')[0]
                    indexer_id = nodes_uuids[node]
                    self.system_events.add_event(IndexingServiceEvents.index_created(node=node,
                                                                                     definition_id=definition_id,
                                                                                     instance_id=instance_id,
                                                                                     indexer_id=indexer_id,
                                                                                     replica_id=replica_id))
                    if not self.defer_build:
                        self.system_events.add_event(
                            IndexingServiceEvents.index_building(node=node, definition_id=definition_id,
                                                                 instance_id=instance_id, indexer_id=indexer_id,
                                                                 replica_id=replica_id))
                        self.system_events.add_event(
                            IndexingServiceEvents.index_online(node=node, definition_id=definition_id,
                                                               instance_id=instance_id, indexer_id=indexer_id,
                                                               replica_id=replica_id))
            result = self.system_events.validate(server=self.master, ignore_order=True)
            if result:
                self.log.error(result)
                self.fail("System Event validation failed")

        elif task == 'free_tier':
            expected_err = 'Limit for number of indexes that can be created per scope has been reached'
            self.rest.set_internalSetting('enforceLimits', True)
            self.updated_tier_limit = 5
            namespace = self.namespaces[0]
            _, keyspace = namespace.split(':')
            bucket, scope, collection = keyspace.split('.')
            self.index_rest.set_gsi_tier_limit(bucket=bucket, scope=scope,
                                               limit=self.updated_tier_limit)
            try:
                for item in range(5):
                    index_name = f'tier_limit_idx_{item}'
                    query = f'create index {index_name} on {namespace}(age) with {{"num_replica": 1}}'
                    self.run_cbq_query(server=self.n1ql_node, query=query)
            except Exception as err:
                if expected_err not in str(err):
                    self.fail(err)

        elif task == 'auto_failover':
            self.enable_autofailover_and_validate()
            self.sleep(5)
            self.failover_actions[self.failover_action](self)
            try:
                self.disable_autofailover_and_validate()
            except Exception as err:
                pass
            if not self.deny_autofailover:
                self.bring_back_failed_nodes_up()
                self.sleep(30)
                self.log.info(self.server_to_fail[0])
                self.nodes = self.rest.node_statuses()
                self.log.info(self.nodes[0].id)
                self.rest.add_back_node(f"ns_1@{self.server_to_fail[0].ip}")
                self.rest.set_recovery_type(f"ns_1@{self.server_to_fail[0].ip}",
                                            self.recovery_strategy)
                self.rest.rebalance(otpNodes=[node.id for node in self.nodes])
                msg = f"rebalance failed while recovering failover nodes {self.server_to_fail[0]}"
                self.assertTrue(self.rest.monitorRebalance(stop_if_loop=True), msg)

        elif task == 'smart_batching':
            add_nodes = self.servers[self.nodes_init:]
            services = ['index'] * len(add_nodes)
            rebalance_task = self.cluster.async_rebalance(servers=self.servers[:self.nodes_init], to_add=add_nodes,
                                                          to_remove=[], services=services)
            self.validate_smart_batching_during_rebalance(rebalance_task)

    def test_online_upgrade(self):
        services_in = []
        before_tasks = self.async_run_operations(buckets=self.buckets, phase="before")
        server_out = self.nodes_out_list
        self._run_tasks([before_tasks])
        in_between_tasks = self.async_run_operations(buckets=self.buckets, phase="in_between")
        kv_ops = self.kv_mutations()
        log.info("Upgrading servers to {0}...".format(self.upgrade_to))
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [], self.nodes_out_list)
        rebalance.result()
        self.upgrade_servers = self.nodes_out_list
        upgrade_th = self._async_update(self.upgrade_to, server_out)
        for th in upgrade_th:
            th.join()
        log.info("==== Upgrade Complete ====")
        self.sleep(120)
        node_version = RestConnection(server_out[0]).get_nodes_versions()
        for service in list(self.services_map.keys()):
            for node in self.nodes_out_list:
                node = "{0}:{1}".format(node.ip, node.port)
                if node in self.services_map[service]:
                    services_in.append(service)
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 self.nodes_out_list, [],
                                                 services=services_in)
        rebalance.result()
        self._run_tasks([kv_ops, in_between_tasks])
        self.sleep(60)
        log.info("Upgraded to: {0}".format(node_version))
        nodes_out = []
        for service in self.nodes_out_dist.split("-"):
            nodes_out.append(service.split(":")[0])
        if "index" in nodes_out or "n1ql" in nodes_out:
            self._verify_bucket_count_with_index_count(query_definitions=self.load_query_definitions)
        else:
            self._verify_bucket_count_with_index_count()
        after_tasks = self.async_run_operations(buckets=self.buckets, phase="after")
        self.sleep(180)
        self._run_tasks([after_tasks])
        self._mixed_mode_tasks()

    def test_online_upgrade_swap_rebalance(self):
        """
        :return:
        """
        before_tasks = self.async_run_operations(buckets=self.buckets, phase="before")
        self._run_tasks([before_tasks])
        self._install(self.nodes_in_list, version=self.upgrade_to)
        in_between_tasks = self.async_run_operations(buckets=self.buckets, phase="in_between")
        kv_ops = self.kv_mutations()
        log.info("Swapping servers...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 self.nodes_in_list,
                                                 self.nodes_out_list)
        rebalance.result()
        log.info("===== Nodes Swapped with Upgraded versions =====")
        self.upgrade_servers = self.nodes_in_list
        self._run_tasks([kv_ops, in_between_tasks])
        self.sleep(60)
        nodes_out = []
        for service in self.nodes_out_dist.split("-"):
            nodes_out.append(service.split(":")[0])
        if "index" in nodes_out or "n1ql" in nodes_out:
            self._verify_bucket_count_with_index_count(query_definitions=self.load_query_definitions)
        else:
            self._verify_bucket_count_with_index_count()
        after_tasks = self.async_run_operations(buckets=self.buckets, phase="after")
        self.sleep(180)
        self._run_tasks([after_tasks])

    def test_online_upgrade_with_rebalance(self):
        before_tasks = self.async_run_operations(buckets=self.buckets,
                                                 phase="before")
        self._run_tasks([before_tasks])
        community_to_enterprise = (self.upgrade_build_type == "enterprise" and self.initial_build_type == "community")
        self._install(self.nodes_in_list, version=self.upgrade_to, community_to_enterprise=community_to_enterprise)
        for i in range(len(self.nodes_out_list)):
            node = self.nodes_out_list[i]
            node_rest = RestConnection(node)
            node_info = "{0}:{1}".format(node.ip, node.port)
            node_services_list = node_rest.get_nodes_services()[node_info]
            node_services = [",".join(node_services_list)]
            active_nodes = []
            for active_node in self.servers:
                if active_node.ip != node.ip:
                    active_nodes.append(active_node)
            in_between_tasks = self.async_run_operations(buckets=self.buckets,
                                                         phase="in_between")
            kv_ops = self.kv_mutations()
            if "index" in node_services_list:
                self._create_equivalent_indexes(node)
            if "n1ql" in node_services_list:
                n1ql_nodes = self.get_nodes_from_services_map(service_type="n1ql",
                                                              get_all_nodes=True)
                if len(n1ql_nodes) > 1:
                    for n1ql_node in n1ql_nodes:
                        if node.ip != n1ql_node.ip:
                            self.n1ql_node = n1ql_node
                            break
            rebalance = self.cluster.async_rebalance(active_nodes,
                                                     [self.nodes_in_list[i]], [],
                                                     services=node_services)
            rebalance.result()
            log.info("===== Node Rebalanced In with Upgraded version =====")
            self._run_tasks([kv_ops, in_between_tasks])
            rebalance = self.cluster.async_rebalance(active_nodes, [], [node])
            rebalance.result()
            if "index" in node_services_list:
                self._verify_indexer_storage_mode(self.nodes_in_list[i])
            self._verify_bucket_count_with_index_count()
            self.multi_query_using_index()
        after_tasks = self.async_run_operations(buckets=self.buckets, phase="after")
        self._run_tasks([after_tasks])
        self._mixed_mode_tasks()

    def test_online_upgrade_with_failover(self):
        before_tasks = self.async_run_operations(buckets=self.buckets,
                                                 phase="before")
        self._run_tasks([before_tasks])
        prepare_statements = self._create_prepare_statement()
        if self.rebalance_empty_node:
            self._install(self.nodes_in_list, version=self.upgrade_to)
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                     [self.nodes_in_list[0]], [],
                                                     services=["index"])
            rebalance.result()
        for i in range(len(self.nodes_out_list)):
            if self.rebalance_empty_node:
                self.disable_upgrade_to_plasma(self.nodes_in_list[0])
                self.set_batch_size(self.nodes_in_list[0], self.index_batch_size)
            node = self.nodes_out_list[i]
            node_rest = RestConnection(node)
            node_info = "{0}:{1}".format(node.ip, node.port)
            node_services_list = node_rest.get_nodes_services()[node_info]
            node_services = [",".join(node_services_list)]
            active_nodes = []
            for active_node in self.servers:
                if active_node.ip != node.ip:
                    active_nodes.append(active_node)
            in_between_tasks = self.async_run_operations(buckets=self.buckets,
                                                         phase="in_between")
            kv_ops = self.kv_mutations()
            if "index" in node_services_list:
                if self.initial_version < "5":
                    self._create_equivalent_indexes(node)
            if "n1ql" in node_services_list:
                n1ql_nodes = self.get_nodes_from_services_map(service_type="n1ql",
                                                              get_all_nodes=True)
                if len(n1ql_nodes) > 1:
                    for n1ql_node in n1ql_nodes:
                        if node.ip != n1ql_node.ip:
                            self.n1ql_node = n1ql_node
                            break
            failover_task = self.cluster.async_failover(
                [self.master],
                failover_nodes=[node],
                graceful=False)
            failover_task.result()
            log.info("Node Failed over...")
            upgrade_th = self._async_update(self.upgrade_to, [node])
            for th in upgrade_th:
                th.join()
            log.info("==== Upgrade Complete ====")
            self.sleep(120)
            rest = RestConnection(self.master)
            nodes_all = rest.node_statuses()
            for cluster_node in nodes_all:
                if cluster_node.ip == node.ip:
                    log.info("Adding Back: {0}".format(node))
                    rest.add_back_node(cluster_node.id)
                    rest.set_recovery_type(otpNode=cluster_node.id,
                                           recoveryType="full")
            log.info("Adding node back to cluster...")
            rebalance = self.cluster.async_rebalance(active_nodes, [], [])
            rebalance.result()
            self._run_tasks([kv_ops, in_between_tasks])
            ops_map = self.generate_operation_map("before")
            if "index" in node_services:
                if self.initial_version < "5":
                    self._remove_equivalent_indexes(node)
                    self.sleep(60)
                self._verify_indexer_storage_mode(node)
                self._verify_throttling(node)
            self.wait_until_indexes_online()
            if self.index_batch_size != 0:
                count = 0
                verify_items = False
                while count < 15 and not verify_items:
                    try:
                        self._verify_bucket_count_with_index_count()
                        verify_items = True
                    except Exception as e:
                        msg = "All Items didn't get Indexed"
                        if msg in str(e) and count < 15:
                            count += 1
                            self.sleep(20)
                        else:
                            raise e
                self.multi_query_using_index()
                self._execute_prepare_statement(prepare_statements)
        self._mixed_mode_tasks()

    def test_online_upgrade_with_rebalance_failover(self):
        nodes_out_list = copy.deepcopy(self.nodes_out_list)
        self.nodes_out_list = []
        self.nodes_out_list.append(nodes_out_list[0])
        self.test_online_upgrade_with_rebalance()
        self.multi_drop_index()
        if self.toggle_disable_upgrade:
            self.disable_plasma_upgrade = not self.toggle_disable_upgrade
        self.nodes_out_list.append(nodes_out_list[1])
        self.test_online_upgrade_with_failover()

    def test_downgrade_plasma_to_fdb_failover(self):
        before_tasks = self.async_run_operations(buckets=self.buckets,
                                                 phase="before")
        self._run_tasks([before_tasks])
        for server in self.servers:
            remote = RemoteMachineShellConnection(server)
            remote.stop_server()
            remote.disconnect()
            self.upgrade_servers.append(server)
        upgrade_threads = self._async_update(self.upgrade_to, self.servers)
        for upgrade_thread in upgrade_threads:
            upgrade_thread.join()
        self.sleep(120)
        self.add_built_in_server_user()
        indexer_node = self.get_nodes_from_services_map(service_type="index")
        rest = RestConnection(indexer_node)
        rest.set_downgrade_storage_mode_with_rest(self.disable_plasma_upgrade)
        failover_task = self.cluster.async_failover(
            [self.master],
            failover_nodes=[indexer_node],
            graceful=False)
        failover_task.result()
        log.info("Node Failed over...")
        rest = RestConnection(self.master)
        nodes_all = rest.node_statuses()
        for cluster_node in nodes_all:
            if cluster_node.ip == indexer_node.ip:
                log.info("Adding Back: {0}".format(indexer_node))
                rest.add_back_node(cluster_node.id)
                rest.set_recovery_type(otpNode=cluster_node.id,
                                       recoveryType="full")
        log.info("Adding node back to cluster...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [], [])
        rebalance.result()
        self.sleep(20)
        self._verify_indexer_storage_mode(indexer_node)
        self.multi_query_using_index()

    def test_downgrade_plasma_to_fdb_rebalance(self):
        before_tasks = self.async_run_operations(buckets=self.buckets,
                                                 phase="before")
        self._run_tasks([before_tasks])
        for server in self.servers:
            remote = RemoteMachineShellConnection(server)
            remote.stop_server()
            remote.disconnect()
            self.upgrade_servers.append(server)
        upgrade_threads = self._async_update(self.upgrade_to, self.servers)
        for upgrade_thread in upgrade_threads:
            upgrade_thread.join()
        self.sleep(120)
        self.add_built_in_server_user()
        for indexer_node in self.nodes_in_list:
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                     [indexer_node], [],
                                                     services=["index"])
            rebalance.result()
            rest = RestConnection(indexer_node)
            rest.set_downgrade_storage_mode_with_rest(self.disable_plasma_upgrade)
            deploy_node_info = ["{0}:{1}".format(indexer_node.ip,
                                                 indexer_node.port)]
            for bucket in self.buckets:
                for query_definition in self.query_definitions:
                    query_definition.index_name = query_definition.index_name + "_replica"
                    self.create_index(bucket=bucket, query_definition=query_definition,
                                      deploy_node_info=deploy_node_info)
                    self.sleep(20)
            self._verify_indexer_storage_mode(indexer_node)
            self.multi_query_using_index()
            self._remove_equivalent_indexes(indexer_node)
            self.disable_plasma_upgrade = not self.disable_plasma_upgrade

    def test_upgrade_with_memdb(self):
        """
        Keep N1ql node on one of the kv nodes
        :return:
        """
        self.set_circular_compaction = self.input.param("set_circular_compaction", False)
        kv_nodes = self.get_nodes_from_services_map(service_type="kv", get_all_nodes=True)
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        index_rest = RestConnection(index_node)
        pre_upgrade_index_stats = index_rest.get_all_index_stats()
        log.info("Upgrading all kv nodes...")
        for node in kv_nodes:
            log.info("Rebalancing kv node {0} out to upgrade...".format(node.ip))
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [],
                                                     [node])
            rebalance.result()
            self.servers.remove(node)
            upgrade_th = self._async_update(self.upgrade_to, [node])
            for th in upgrade_th:
                th.join()
            self.sleep(120)
            log.info("Rebalancing kv node {0} in after upgrade...".format(node.ip))
            rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                     [node], [],
                                                     services=['kv'])
            self.servers.insert(0, node)
            rebalance.result()
        log.info("===== KV Nodes Upgrade Complete =====")
        log.info("Upgrading all query nodes...")
        query_nodes = self.get_nodes_from_services_map(service_type="n1ql", get_all_nodes=True)
        log.info("Rebalancing query nodes out to upgrade...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [],
                                                 query_nodes)
        rebalance.result()
        upgrade_th = self._async_update(self.upgrade_to, query_nodes)
        for th in upgrade_th:
            th.join()
        self.sleep(120)
        services_in = ["n1ql" for x in range(len(query_nodes))]
        log.info("Rebalancing query nodes in after upgrade...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 query_nodes, [],
                                                 services=services_in)
        rebalance.result()
        log.info("===== Query Nodes Upgrade Complete =====")
        kv_ops = self.kv_mutations()
        log.info("Upgrading all index nodes...")
        index_nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
        log.info("Rebalancing index nodes out to upgrade...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [],
                                                 index_nodes)
        rebalance.result()
        upgrade_th = self._async_update(self.upgrade_to, index_nodes)
        self.sleep(120)
        rest = RestConnection(self.master)
        log.info("Setting indexer storage mode to {0}...".format(self.post_upgrade_gsi_type))
        status = rest.set_indexer_storage_mode(storageMode=self.post_upgrade_gsi_type)
        if status:
            log.info("====== Indexer Mode Set to {0}=====".format(self.post_upgrade_gsi_type))
        else:
            log.info("====== Indexer Mode is not set to {0}=====".format(self.post_upgrade_gsi_type))
        for th in upgrade_th:
            th.join()
        self._run_tasks([kv_ops])
        log.info("===== Index Nodes Upgrade Complete =====")
        services_in = ["index" for x in range(len(index_nodes))]
        log.info("Rebalancing index nodes in after upgrade...")
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 index_nodes, [],
                                                 services=services_in)
        rebalance.result()
        self.sleep(60)
        if self.set_circular_compaction:
            DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            servers = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
            rest = RestConnection(servers[0])
            date = datetime.now()
            dayOfWeek = (date.weekday() + (date.hour + ((date.minute + 5) // 60)) // 24) % 7
            status, content, header = rest.set_indexer_compaction(indexDayOfWeek=DAYS[dayOfWeek],
                                                                  indexFromHour=date.hour + ((date.minute + 1) // 60),
                                                                  indexFromMinute=(date.minute + 1) % 60)
            self.assertTrue(status, "Error in setting Circular Compaction... {0}".format(content))
        self.multi_create_index(self.buckets, self.query_definitions)
        self._verify_bucket_count_with_index_count()
        self.multi_query_using_index(self.buckets, self.query_definitions)

        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        index_rest = RestConnection(index_node)
        post_upgrade_index_stats = index_rest.get_all_index_stats()
        # Only one Index node, can't compare Index stats
        # self._post_upgrade_task(task='stats_comparison', stats_before_upgrade=pre_upgrade_index_stats,
        #                         stats_after_upgrade=post_upgrade_index_stats)
        self._post_upgrade_task(task='create_collection')
        self._post_upgrade_task(task='auto_failover')
        self._post_upgrade_task(task='create_indexes')
        if self.enable_dgm:
            self.assertTrue(self._is_dgm_reached())
        self._post_upgrade_task(task='run_query')
        self.log.info(f"Rebalancing in new node - {self.servers[self.nodes_init]}")
        self._post_upgrade_task(task='rebalance_in', node=self.servers[self.nodes_init])
        self._post_upgrade_task(task='rebalance_out', node=self.servers[self.nodes_init])
        self._post_upgrade_task(task='drop_all_indexes')
        # creating indexes again to check plasma sharding
        self._post_upgrade_task(task='create_indexes')

        # Neo Features
        self._post_upgrade_task(task='smart_batching')
        self._post_upgrade_task(task='system_event')
        self._post_upgrade_task(task='free_tier')


    def test_online_upgrade_path_with_rebalance(self):
        pre_upgrade_tasks = self.async_run_operations(phase="before")
        self._run_tasks([pre_upgrade_tasks])
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        index_rest = RestConnection(index_node)
        pre_upgrade_index_stats = index_rest.get_all_index_stats()
        threads = [Thread(target=self._async_continuous_queries, name="run_query")]
        kvOps_tasks = self.async_run_doc_ops()
        for thread in threads:
            thread.start()
        self.nodes_upgrade_path = self.input.param("nodes_upgrade_path", "").split("-")
        for service in self.nodes_upgrade_path:
            nodes = self.get_nodes_from_services_map(service_type=service, get_all_nodes=True)
            log.info("----- Upgrading all {0} nodes -----".format(service))
            for node in nodes:
                node_rest = RestConnection(node)
                node_info = "{0}:{1}".format(node.ip, node.port)
                node_services_list = node_rest.get_nodes_services()[node_info]
                node_services = [",".join(node_services_list)]
                if "index" in node_services_list:
                    if len(nodes) == 1:
                        threads = []
                    else:
                        self._create_equivalent_indexes(node)
                if "n1ql" in node_services_list:
                    if len(nodes) > 1:
                        for n1ql_node in nodes:
                            if node.ip != n1ql_node.ip:
                                self.n1ql_node = n1ql_node
                                break
                log.info("Rebalancing the node out...")
                rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [], [node])
                rebalance.result()
                active_nodes = []
                for active_node in self.servers:
                    if active_node.ip != node.ip:
                        active_nodes.append(active_node)
                log.info("Upgrading the node...")
                upgrade_th = self._async_update(self.upgrade_to, [node])
                for th in upgrade_th:
                    th.join()
                self.sleep(120)
                log.info("==== Upgrade Complete ====")
                log.info("Adding node back to cluster...")
                rebalance = self.cluster.async_rebalance(active_nodes,
                                                         [node], [],
                                                         services=node_services)
                rebalance.result()
                # self.sleep(100)
                node_version = RestConnection(node).get_nodes_versions()
                log.info("{0} node {1} Upgraded to: {2}".format(service, node.ip, node_version))
                ops_map = self.generate_operation_map("in_between")
                if not "drop_index" in ops_map:
                    if "index" in node_services_list:
                        self._recreate_equivalent_indexes(node)
                else:
                    self.multi_create_index()
                self._verify_scan_api()
                self._create_replica_indexes(keyspace='standard_bucket0')
                self.multi_query_using_index(verify_results=False)
                if "create_index" in ops_map:
                    for bucket in self.buckets:
                        for query_definition in self.query_definitions:
                            self.drop_index(bucket.name, query_definition)
        self._run_tasks([kvOps_tasks])
        for thread in threads:
            thread.join()
        self.sleep(60)
        buckets = self._create_plasma_buckets()
        self.load(self.gens_load, buckets=buckets, flag=self.item_flag, batch_size=self.batch_size)
        self.multi_create_index(buckets=buckets, query_definitions=self.query_definitions)
        self.multi_query_using_index(buckets=buckets, query_definitions=self.query_definitions)
        self._verify_index_partitioning()
        index_nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
        index_rest = RestConnection(index_nodes[0])
        post_upgrade_index_stats = index_rest.get_all_index_stats()

        self._post_upgrade_task(task='stats_comparison', stats_before_upgrade=pre_upgrade_index_stats,
                                stats_after_upgrade=post_upgrade_index_stats)
        self._post_upgrade_task(task='create_collection')
        if self.num_index_replicas > 0:
            index_nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
            if len(index_nodes) > 1:
                self._post_upgrade_task(task='auto_failover')
            else:
                self.log.info("Can't run Auto-Failover tests for one Index node")
        self._post_upgrade_task(task='create_indexes')
        if self.enable_dgm:
            self.assertTrue(self._is_dgm_reached())
        self._post_upgrade_task(task='run_query')
        self._post_upgrade_task(task='request_plus_scans')
        self.log.info(f"Rebalancing in new node - {self.servers[self.nodes_init]}")
        self._post_upgrade_task(task='rebalance_in', node=self.servers[self.nodes_init])
        if len(index_nodes) > 1:
            self._post_upgrade_task(task='rebalance_out', node=index_nodes[0])
        self._post_upgrade_task(task='drop_all_indexes')
        # creating indexes again to check plasma sharding
        self._post_upgrade_task(task='create_indexes')

        # Neo Features
        self._post_upgrade_task(task='smart_batching')
        self._post_upgrade_task(task='system_event')
        self._post_upgrade_task(task='free_tier')

    def kv_mutations(self, docs=None):
        if not docs:
            docs = self.docs_per_day
        gens_load = self.generate_docs(docs)
        tasks = self.async_load(generators_load=gens_load, batch_size=self.batch_size)
        return tasks

    def _is_dgm_reached(self):
        index_node = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)[0]
        index_rest = RestConnection(index_node)
        stats = index_rest.get_all_index_stats()
        for key in stats:
            if ':resident_percent' in key:
                if stats[key] < 100:
                    self.log.info("DGM achieved")
                    return True
        else:
            self.log.error(stats)
            return False

    def _run_tasks(self, tasks_list):
        for tasks in tasks_list:
            for task in tasks:
                task.result()

    def _verify_create_index_api(self, keyspace='default'):
        """
        1. Get Indexer and Query Versions
        2. Run create query with explain
        3. Verify the api returned
        :return:
        """
        old_api = False
        node_map = self._get_nodes_with_version()
        log.info(node_map)
        for node, vals in node_map.items():
            if vals["version"] < "5":
                old_api = True
                break
        create_index_query_age = f"CREATE INDEX verify_api ON {keyspace}(age DESC)"
        try:
            query_result = self.n1ql_helper.run_cbq_query(query=create_index_query_age,
                                                          server=self.n1ql_node)
        except Exception as ex:
            if old_api:
                msgs = ["'syntax error - at DESC'",
                        "This option is enabled after cluster is fully upgraded and there is no failed node"]
                desc_error_hit = False
                for msg in msgs:
                    if msg in str(ex):
                        desc_error_hit = True
                        break
                if not desc_error_hit:
                    log.info(str(ex))
                    raise
            else:
                log.info(str(ex))
                raise

    def _verify_scan_api(self):
        """
        1. Get Indexer and Query Versions
        2. Run create query with explain
        3. Verify the api returned
        :return:
        """
        node_map = self._get_nodes_with_version()
        for query_definition in self.query_definitions:
            query = query_definition.generate_query_with_explain(bucket=self.buckets[0])
            actual_result = self.n1ql_helper.run_cbq_query(query=query, server=self.n1ql_node)
            log.info(actual_result)
            old_api = False
            api_two = False
            for node, vals in node_map.items():
                if vals["version"] < "5":
                    old_api = True
                    break
                elif vals["version"] < "5.5":
                    api_two = True
            if not old_api and api_two:
                msg = "IndexScan2"
                self.assertIn(msg, str(actual_result), "IndexScan2 is not used for Spock Nodes")
            elif not old_api and not api_two:
                msg = "IndexScan3"
                self.assertIn(msg, str(actual_result), "IndexScan3 is not used for Vulcan Nodes")

    def _create_replica_indexes(self, keyspace='default'):
        nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
        create_index_query = f"CREATE INDEX index_replica_index ON {keyspace}(age) USING GSI  WITH {{'num_replica': {len(nodes) - 1}}};"
        try:
            query_result = self.n1ql_helper.run_cbq_query(query=create_index_query,
                                                          server=self.n1ql_node)
        except Exception as ex:
            old_api = False
            node_map = self._get_nodes_with_version()
            log.info(node_map)
            for node, vals in node_map.items():
                if vals["version"] < "5":
                    old_api = True
                    msg = "Fails to create index with replica"
                    if msg in str(ex):
                        break
            if not old_api:
                log.info(str(ex))
                raise
        else:
            drop_index_query = f"DROP INDEX {keyspace}.index_replica_index"
            query_result = self.n1ql_helper.run_cbq_query(query=drop_index_query,
                                                          server=self.n1ql_node)

    def _recreate_equivalent_indexes(self, index_node):
        node_map = self._get_nodes_with_version()
        for node, vals in node_map.items():
            if vals["version"] < "5":
                rest = RestConnection(self.master)
                index_map = rest.get_index_status()
                log.info(index_map)
                lost_indexes = {}
                for bucket, index in index_map.items():
                    for index, vals in index.items():
                        if "_replica" in index:
                            if not index in list(lost_indexes.keys()):
                                lost_indexes[index] = []
                            lost_indexes[index].append(bucket)
                deploy_node_info = ["{0}:{1}".format(index_node.ip, index_node.port)]
                for index, buckets in lost_indexes.items():
                    for query_definition in self.query_definitions:
                        if query_definition.index_name == index:
                            query_definition.index_name = query_definition.index_name.split("_replica")[0]
                            for bucket in buckets:
                                bucket = [x for x in self.buckets if x.name == bucket][0]
                                self.create_index(bucket=bucket,
                                                  query_definition=query_definition,
                                                  deploy_node_info=deploy_node_info)
                                self.sleep(20)
                            query_definition.index_name = index
                            for bucket in buckets:
                                bucket = [x for x in self.buckets if x.name == bucket][0]
                                self.drop_index(bucket, query_definition)
                                self.sleep(20)
                            query_definition.index_name = query_definition.index_name.split("_replica")[0]

    def _remove_equivalent_indexes(self, index_node):
        node_map = self._get_nodes_with_version()
        for node, vals in node_map.items():
            if vals["version"] > "5":
                rest = RestConnection(self.master)
                index_map = rest.get_index_status()
                log.info(index_map)
                for query_definition in self.query_definitions:
                    if "_replica" in query_definition.index_name:
                        for bucket in self.buckets:
                            self.drop_index(bucket, query_definition)
                            self.sleep(20)
                        query_definition.index_name = query_definition.index_name.split("_replica")[0]

    def _create_equivalent_indexes(self, index_node):
        node_map = self._get_nodes_with_version()
        for node, vals in node_map.items():
            if vals["version"] < "5":
                index_nodes = self.get_nodes_from_services_map(service_type="index",
                                                               get_all_nodes=True)
                index_nodes = [x for x in index_nodes if x.ip != index_node.ip]
                if index_nodes:
                    ops_map = self.generate_operation_map("in_between")
                    if "create_index" not in ops_map:
                        lost_indexes = self._find_index_lost_when_indexer_down(index_node)
                        deploy_node_info = ["{0}:{1}".format(index_nodes[0].ip,
                                                             index_nodes[0].port)]
                        for index, buckets in lost_indexes.items():
                            for query_definition in self.query_definitions:
                                if query_definition.index_name == index:
                                    query_definition.index_name = query_definition.index_name + "_replica"
                                    for bucket in buckets:
                                        bucket = [x for x in self.buckets if x.name == bucket][0]
                                        self.create_index(bucket=bucket,
                                                          query_definition=query_definition,
                                                          deploy_node_info=deploy_node_info)
                                        self.sleep(20)

    def _find_index_lost_when_indexer_down(self, index_node):
        lost_indexes = {}
        rest = RestConnection(self.master)
        index_map = rest.get_index_status()
        log.info("index_map: {0}".format(index_map))
        if self.use_https:
            port = '18091'
        else:
            port = '8091'
        host = "{0}:{1}".format(index_node.ip, port)
        for bucket, index in index_map.items():
            for index, vals in index.items():
                if vals["hosts"] == host:
                    if not index in list(lost_indexes.keys()):
                        lost_indexes[index] = []
                    lost_indexes[index].append(bucket)
        log.info("Lost Indexes: {0}".format(lost_indexes))
        return lost_indexes

    def _get_nodes_with_version(self):
        rest_conn = RestConnection(self.master)
        nodes = rest_conn.get_nodes()
        map = {}
        for cluster_node in nodes:
            map[cluster_node.ip] = {"version": cluster_node.version,
                                    "services": cluster_node.services}
        return map

    def _create_prepare_statement(self):
        prepare_name_query = {}
        for bucket in self.buckets:
            prepare_name_query[bucket.name] = {}
            for query_definition in self.query_definitions:
                query = query_definition.generate_query(bucket=bucket)
                name = "prepare_" + query_definition.index_name + bucket.name
                query = "PREPARE " + name + " FROM " + query
                result = self.n1ql_helper.run_cbq_query(query=query, server=self.n1ql_node)
                self.assertEqual(result['status'], 'success', 'Query was not run successfully')
                prepare_name_query[bucket.name][query_definition.index_name] = name
        return prepare_name_query

    def _execute_prepare_statement(self, prepare_name_query):
        for bucket in self.buckets:
            for query_definition in self.query_definitions:
                prepared_query = "EXECUTE " + prepare_name_query[bucket.name][query_definition.index_name]
                result = self.n1ql_helper.run_cbq_query(query=prepared_query, server=self.n1ql_node)
                self.assertEqual(result['status'], 'success', 'Query was not run successfully')

    def _async_continuous_queries(self):
        tasks = []
        for i in range(100):
            mid_upgrade_tasks = self.async_run_operations(phase="in_between")
            tasks.append(mid_upgrade_tasks)
            self.sleep(10)
        return tasks

    def _create_plasma_buckets(self):
        self.add_built_in_server_user()
        for bucket in self.buckets:
            if bucket.name.startswith("standard"):
                BucketOperationHelper.delete_bucket_or_assert(
                    serverInfo=self.master, bucket=bucket.name)
        self.buckets = [bu for bu in self.buckets if not bu.name.startswith("standard")]
        buckets = []
        for i in range(self.num_plasma_buckets):
            name = "plasma_bucket_" + str(i)
            buckets.append(name)
        bucket_size = self._get_bucket_size(self.quota,
                                            len(self.buckets) + len(buckets))
        self._create_buckets(server=self.master, bucket_list=buckets,
                             bucket_size=bucket_size)
        testuser = []
        rolelist = []
        for bucket in buckets:
            testuser.append({'id': bucket, 'name': bucket, 'password': 'password'})
            rolelist.append({'id': bucket, 'name': bucket, 'roles': 'admin'})
        self.add_built_in_server_user(testuser=testuser, rolelist=rolelist)
        buckets = []
        for bucket in self.buckets:
            if bucket.name.startswith("plasma_bucket"):
                buckets.append(bucket)
        return buckets

    def _verify_gsi_rebalance(self):
        node_map = self._get_nodes_with_version()
        for node, vals in node_map.items():
            if vals["version"] < "5":
                return
        self.rest = RestConnection(self.master)
        map_before_rebalance, stats_map_before_rebalance = self._return_maps()
        nodes_out_list = self.get_nodes_from_services_map(service_type="index")
        # rebalance out a node
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init], [], [nodes_out_list])
        reached = RestHelper(self.rest).rebalance_reached()
        rebalance.result()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")

        self.sleep(30)
        map_after_rebalance, stats_map_after_rebalance = self._return_maps()
        self.n1ql_helper.verify_indexes_redistributed(
            map_before_rebalance, map_after_rebalance, stats_map_before_rebalance,
            stats_map_after_rebalance, [], [nodes_out_list])

        # Add back the node that was removed, and use alter index to move an index to that node
        rebalance = self.cluster.async_rebalance(self.servers[:self.nodes_init],
                                                 [nodes_out_list], [], services=["kv,index,n1ql"])
        reached = RestHelper(self.rest).rebalance_reached()
        rebalance.result()
        self.assertTrue(reached, "rebalance failed, stuck or did not complete")
        self._verify_alter_index()
        self.sleep(120)

    def _verify_alter_index(self):
        index_nodes = self.get_nodes_from_services_map(service_type="index", get_all_nodes=True)
        rest = RestConnection(self.master)
        index_map = rest.get_index_status()
        log.info("index_map: {0}".format(index_map))
        index_info = index_map[self.buckets[0].name]
        for index_name, index_vals in index_info.items():
            host = index_vals["hosts"]
            for index_node in index_nodes:
                ip_str = index_node.ip + ":" + index_node.port
                if host != ip_str:
                    alter_index_query = "ALTER INDEX {0}.{1} with {{'action':'move','nodes':['{2}:{3}']}}".format(
                        self.buckets[0].name, index_name, index_node.ip, index_node.port)
                    result = self.n1ql_helper.run_cbq_query(query=alter_index_query, server=self.n1ql_node)
                    self.assertEqual(result['status'], 'success', 'Query was not run successfully')
                    return

    def _verify_index_partitioning(self):
        node_map = self._get_nodes_with_version()
        for node, vals in node_map.items():
            if vals["version"] < "5.5":
                return
        indexer_node = self.get_nodes_from_services_map(service_type="index")
        # Set indexer storage mode
        rest = RestConnection(indexer_node)
        rest.set_index_settings({"indexer.numPartitions": 2})

        queries = []
        for bucket in self.buckets:
            create_partitioned_index1_query = f"CREATE INDEX partitioned_idx1 ON {bucket.name}(name, age, join_yr) " \
                                              f"partition by hash(name, age, join_yr) "
            create_index1_query = f"CREATE INDEX non_partitioned_idx1 ON {bucket.name}(name, age, join_yr) "

            if self.num_index_replicas > 0:
                create_partitioned_index1_query += f' with {{"num_replica": {self.num_index_replicas} }}'
                create_index1_query += f' with {{"num_replica": {self.num_index_replicas} }}'

            try:
                self.n1ql_helper.run_cbq_query(query=create_partitioned_index1_query, server=self.n1ql_node)
                self.n1ql_helper.run_cbq_query(query=create_index1_query, server=self.n1ql_node)
            except Exception as ex:
                self.log.info(str(ex))
                self.fail(
                    "index creation failed with error : {0}".format(str(ex)))

            # Scans

            # 1. Small lookup query with equality predicate on the partition key
            query_details = {"query": f"select name, age, join_yr from {bucket.name} USE INDEX" +
                                      " ({0}) where name='Kala'",
                             "partitioned_idx_name": "partitioned_idx1",
                             "non_partitioned_idx_name": "non_partitioned_idx1"}
            queries.append(query_details)

            # 2. Pagination query with equality predicate on the partition key
            query_details = {
                "query": f"select name, age, join_yr from {bucket.name} USE INDEX" +
                         " ({0}) where name is not missing AND age=50 offset 0 limit 10",
                "partitioned_idx_name": "partitioned_idx1", "non_partitioned_idx_name": "non_partitioned_idx1"}
            queries.append(query_details)

            # 3. Large aggregated query
            query_details = {
                "query": f"select count(name), age from {bucket.name} USE INDEX" +
                         " ({0}) where name is not missing group by age",
                "partitioned_idx_name": "partitioned_idx1", "non_partitioned_idx_name": "non_partitioned_idx1"}
            queries.append(query_details)

            # 4. Scan with large result sets
            query_details = {
                "query": f"select name, age, join_yr from {bucket.name} USE INDEX" +
                         " ({0}) where name is not missing AND age > 50",
                "partitioned_idx_name": "partitioned_idx1", "non_partitioned_idx_name": "non_partitioned_idx1"}
            queries.append(query_details)

        failed_queries = []
        for query_details in queries:
            try:
                query_partitioned_index = query_details["query"].format(query_details["partitioned_idx_name"])
                query_non_partitioned_index = query_details["query"].format(query_details["non_partitioned_idx_name"])

                result_partitioned_index = self.n1ql_helper.run_cbq_query(query=query_partitioned_index,
                                                                          server=self.n1ql_node)["results"]
                result_non_partitioned_index = self.n1ql_helper.run_cbq_query(query=query_non_partitioned_index,
                                                                              server=self.n1ql_node)["results"]

                if sorted(result_partitioned_index) != sorted(result_non_partitioned_index):
                    failed_queries.append(query_partitioned_index)
                    log.warning("*** This query does not return same results for partitioned and non-partitioned "
                                "indexes.")
            except Exception as ex:
                log.info(str(ex))
        msg = "Some scans did not yield the same results for partitioned index and non-partitioned indexes"
        self.assertEqual(len(failed_queries), 0, msg)

    def _return_maps(self):
        index_map = self.get_index_map()
        stats_map = self.get_index_stats(perNode=False)
        return index_map, stats_map

    def disable_upgrade_to_plasma(self, indexer_node):
        rest = RestConnection(indexer_node)
        doc = {"indexer.settings.storage_mode.disable_upgrade": self.disable_plasma_upgrade}
        rest.set_index_settings(doc)
        self.sleep(10)
        remote = RemoteMachineShellConnection(indexer_node)
        remote.stop_server()
        self.sleep(30)
        remote.start_server()
        self.sleep(30)

    def set_batch_size(self, indexer_node, batch_size=5):
        rest = RestConnection(indexer_node)
        doc = {"indexer.settings.build.batch_size": batch_size}
        rest.set_index_settings(doc)
        self.sleep(10)
        remote = RemoteMachineShellConnection(indexer_node)
        remote.stop_server()
        self.sleep(30)
        remote.start_server()
        self.sleep(30)

    def get_batch_size(self, indexer_node):
        rest = RestConnection(indexer_node)
        json_settings = rest.get_index_settings()
        return json_settings["indexer.settings.build.batch_size"]

    def _verify_indexer_storage_mode(self, indexer_node):
        if self.use_https:
            port = '18091'
        else:
            port = '8091'
        indexer_info = "{0}:{1}".format(indexer_node.ip, port)
        rest = RestConnection(indexer_node)
        index_metadata = rest.get_indexer_metadata()["status"]
        node_map = self._get_nodes_with_version()
        for node in node_map.keys():
            if node == indexer_node.ip:
                if node_map[node]["version"] < "5" or \
                        self.gsi_type == "memory_optimized":
                    return
                else:
                    if self.disable_plasma_upgrade:
                        gsi_type = "forestdb"
                    else:
                        gsi_type = "plasma"
                    for index_val in index_metadata:
                        if index_val["hosts"] == indexer_info:
                            self.assertEqual(index_val["indexType"], gsi_type,
                                             "GSI type is not {0} after upgrade for index {1}".format(gsi_type,
                                                                                                      index_val[
                                                                                                          "name"]))

    def _verify_throttling(self, indexer_node):
        if self.use_https:
            port = '18091'
        else:
            port = '8091'
        indexer_info = "{0}:{1}".format(indexer_node.ip, port)
        rest = RestConnection(indexer_node)
        index_metadata = rest.get_indexer_metadata()["status"]
        index_building = 0
        index_created = 0
        for index_val in index_metadata:
            if index_val["hosts"] == indexer_info:
                index_building = index_building + (index_val["status"].lower() == "building")
                index_created = index_created + (index_val["status"].lower() == "created")
        batch_size = self.get_batch_size(indexer_node)
        self.assertGreaterEqual(batch_size, -1, "Batch size is less than -1. Failing")
        if batch_size == -1:
            self.assertEqual(index_created, 0,
                             "{0} indexes are in created state when batch size is -1".format(index_created))
            return
        if batch_size == 0:
            self.assertEqual(index_created, 0,
                             "{0} indexes are in building when batch size is 0".format(index_building))
            return
        if batch_size > 0:
            self.assertLessEqual(index_building, batch_size,
                                 "{0} indexes are in building when batch size is {1}".format(index_building,
                                                                                             batch_size))
            return
