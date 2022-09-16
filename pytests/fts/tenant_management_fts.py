# coding=utf-8

from .fts_base import FTSBaseTest, FTSIndex


class TenantManagementFTS(FTSBaseTest):

    def setUp(self):
        super(TenantManagementFTS, self).setUp()

    def tearDown(self):
        super(TenantManagementFTS, self).tearDown()

    def suite_setUp(self):
        pass

    def suite_tearDown(self):
        pass

    def create_simple_default_index(self, data_loader_output=False):
        plan_params = self.construct_plan_params()
        self.load_data(generator=None, data_loader_output=data_loader_output, num_items=self._num_items)
        if not (self.bucket_storage == "magma"):
            self.wait_till_items_in_bucket_equal(self._num_items // 2)
        self.create_fts_indexes_all_buckets(plan_params=plan_params)
        if self.restart_couchbase:
            self._cb_cluster.restart_couchbase_on_all_nodes()
        if self._update or self._delete:
            self.wait_for_indexing_complete()
            self.validate_index_count(equal_bucket_doc_count=True,
                                      zero_rows_ok=False)
            self.sleep(20)
            self.async_perform_update_delete(self.upd_del_fields)
            if self._update:
                self.sleep(60, "Waiting for updates to get indexed...")
        self.wait_for_indexing_complete()
        self.validate_index_count(equal_bucket_doc_count=True)

    # Verify that you will not be able to create indexes with more than 1 partition and 1 replica (In both rest and UI)
    def create_simple_default_index_partition_check(self, data_loader_output=False):
        plan_params = self.construct_plan_params()
        self.load_data(generator=None, data_loader_output=data_loader_output, num_items=self._num_items)
        if not (self.bucket_storage == "magma"):
            self.wait_till_items_in_bucket_equal(self._num_items // 2)
        try:
            self.create_fts_indexes_all_buckets(plan_params=plan_params)
        except Exception as e:
            AssertionError(str(e),
                           "limitIndexDef: support for indexes with 1 active + 1 replica partitions only in serverless mode")
            self.fail(
                "Testcase failed: support for indexes with 1 active + 1 replica partitions only in serverless mode")

    # Creating more than 20 indexes per bucket should fail
    def create_max_20index_per_bucket(self, analyzer='standard'):
        plan_params = self.construct_plan_params()
        try:
            self.create_fts_indexes_all_buckets(plan_params=plan_params)
        except:
            self.fail("Testcase failed: Atleast 20 indexes should be created")

        try:
            for bucket in self._cb_cluster.get_buckets():
                collection_index, tp, index_scope, index_collections = self.define_index_parameters_collection_related()
                self.create_index(
                    bucket,
                    f"{bucket.name}_index_{21}",
                    plan_params=plan_params, _type=tp, collection_index=collection_index,
                    scope=index_scope, collections=index_collections, analyzer=analyzer)
            self.fail("Testcase failed: Creating more than 20 indexes per bucket is not permitted")
        except Exception as err:
            self.log.error(err)
            AssertionError(str(err),"limting/throttling: the request has been rejected according to regulator, msg:rejecting create request since index count limit per database has been reached")
            print("Testcase Passed!")

    # Verify that you will not be able to update indexes with more than 1 partition and 1 replica (In both rest and UI)
    def update_simple_default_index_partition_check(self):
        # plan_params = self.construct_plan_params()
        # try:
        #     for bucket in self._cb_cluster.get_buckets():
        #         collection_index, tp, index_scope, index_collections = self.define_index_parameters_collection_related()
        #         self.create_index(
        #             bucket,
        #             "Index123",
        #             plan_params=plan_params, _type=tp, collection_index=collection_index,
        #             scope=index_scope, collections=index_collections, analyzer=analyzer)
        # except:
        #     print("zzzzzzzzzz")

        f = FTSIndex(
            cluster=self._cb_cluster,
            name="Index123",
            source_name="default",
        )
        f.create()

        try:
            f.update_num_replicas(0)
            f.update_num_pindexes(0)
            self.fail("Testcase failed: creating indexes with less than 1 partition and 1 replica is not permitted")
        except:
            print("Testcase passed : denied creating indexes with less than 1 partition and 1 replica ")

        try:
            f.update_num_replicas(2)
            f.update_num_pindexes(2)
            self.fail("Testcase failed: creating indexes with more than 1 partition and 1 replica is not permitted")
        except:
            print("Testcase passed : denied creating indexes with more than 1 partition and 1 replica")

        try:
            f.update_num_replicas(1)
            f.update_num_pindexes(1)
            print("Testcase passed : created index with 1 partition and 1 replica")
        except Exception as err:
            self.log.error(err)
            self.fail("Testcase failed: Test should allow to create indexes with 1 partition and 1 replica" + str(err))
