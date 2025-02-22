from serverless.serverless_basetestcase import ServerlessBaseTestCase
from lib.metering_throttling import metering
import math

class QueryMeterSanity(ServerlessBaseTestCase):
    def setUp(self):
        self.doc_count = 10
        self.scope = '_default'
        self.collection = '_default'
        return super().setUp()

    def tearDown(self):
        return super().tearDown()

    def suite_setUp(self):
        pass

    def suite_tearDown(self):
        pass

    def provision_databases(self, count=1):
        self.log.info(f'PROVISIONING {count} DATABASE/s ...')
        tasks = []
        for _ in range(0, count):
            task = self.create_database_async()
            tasks.append(task)
        for task in tasks:
            task.result()

    def test_meter_write(self):
        self.provision_databases()
        for database in self.databases.values():
            meter = metering(database.rest_host, database.admin_username, database.admin_password)
            before_kv_ru, before_kv_wu = meter.get_kv_rwu(database.id)
            result = self.run_query(database, f'INSERT INTO {self.collection} (key k, value v) select uuid() as k , {{"name": "San Francisco"}} as v from array_range(0,{self.doc_count}) d')
            self.log.info(f"billingUnits: {result['billingUnits']}")
            self.assertEqual(result['billingUnits']['wu']['kv'], self.doc_count)
            after_kv_ru, after_kv_wu = meter.get_kv_rwu(database.id)
            self.assertEqual(after_kv_wu - before_kv_wu, self.doc_count)
            # To-do: Might be impacted by DCP stream right now
            # self.assertEqual(after_kv_ru, before_kv_ru) # no read units

    def test_meter_read(self):
        self.provision_databases()
        for database in self.databases.values():
            result = self.run_query(database, f'INSERT INTO {self.collection} (key k, value v) select uuid() as k , {{"name": "San Francisco"}} as v from array_range(0,{self.doc_count}) d')
            # Get sequential scan read unit
            result = self.run_query(database, f'SELECT meta().id FROM {self.collection}')
            sc_ru = result['billingUnits']['ru']['kv']
            meter = metering(database.rest_host, database.admin_username, database.admin_password)
            before_kv_ru, before_kv_wu = meter.get_kv_rwu(database.id)
            result = self.run_query(database, f'SELECT * FROM {self.collection}')
            self.log.info(f"billingUnits: {result['billingUnits']}")
            self.assertEqual(result['billingUnits']['ru']['kv'], self.doc_count + sc_ru)
            after_kv_ru, after_kv_wu = meter.get_kv_rwu(database.id)
            self.assertEqual(after_kv_ru - before_kv_ru, self.doc_count + sc_ru)
            self.assertEqual(after_kv_wu, before_kv_wu) # no writes

    def test_meter_cu(self):
        self.provision_databases()
        for database in self.databases.values():
            meter = metering(database.rest_host, database.admin_username, database.admin_password)
            expected_cu = 1
            before_cu = meter.get_query_cu(database.id, unbilled='true')
            result = self.run_query(database, 'SELECT 10+10')
            after_cu = meter.get_query_cu(database.id, unbilled='true')
            self.assertEqual(expected_cu, after_cu - before_cu)

            before_cu = meter.get_query_cu(database.id, unbilled='true')
            result = self.run_query(database, 'SELECT array_repeat(array_repeat(repeat("a",100), 500), 1000)')
            after_cu = meter.get_query_cu(database.id, unbilled='true')
            # 1 compute unit (CU) is 32MB per second
            expected_cu = math.ceil(int(result['metrics']['usedMemory']) * float(result['metrics']['executionTime'][:-1]) / (32*1024*1024))
            self.assertEqual (expected_cu, after_cu - before_cu)
