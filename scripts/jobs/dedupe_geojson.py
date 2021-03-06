import ujson as json
from collections import Counter, defaultdict

from lieu.api import DedupeResponse
from lieu.address import Address
from lieu.dedupe import NameDeduper

from lieu.spark.dedupe import AddressDeduperSpark, VenueDeduperSpark
from lieu.spark.utils import IDPairRDD

from mrjob.job import MRJob


class DedupeVenuesJob(MRJob):
    def configure_options(self):
        super(DedupeVenuesJob, self).configure_options()
        self.add_passthrough_option(
            '--address-only',
            default=False,
            action="store_true",
            help="Address duplicates only")

        self.add_passthrough_option(
            '--no-geo-model',
            default=False,
            action="store_true",
            help="Disables the geo model (if using Spark on a small, local data set)")

        self.add_passthrough_option(
            '--dupes-only',
            action='store_true',
            default=False,
            help='Only output the dupes')

        self.add_passthrough_option(
            '--no-latlon',
            action='store_true',
            default=False,
            help='Do not use lat/lon or geohashing (if one data set has no lat/lons for instance)')

        self.add_passthrough_option(
            '--use-city',
            action='store_true',
            default=False,
            help='Use the city for cases where lat/lon is not available (only for local data sets)')

        self.add_passthrough_option(
            '--use-postal-code',
            action='store_true',
            default=False,
            help='Use the postcode when lat/lon is not available')

        self.add_passthrough_option(
            '--name-dupe-threshold',
            type='float',
            default=DedupeResponse.default_name_dupe_threshold,
            help='Likely-dupe threshold between 0 and 1 for name deduping with Soft-TFIDF')

        self.add_passthrough_option(
            '--name-review-threshold',
            type='float',
            default=DedupeResponse.default_name_review_threshold,
            help='Human review threshold between 0 and 1 for name deduping with Soft-TFIDF')

        self.add_passthrough_option(
            '--with-unit',
            default=False,
            action="store_true",
            help="Whether to include units in deduplication")

    def spark(self, input_path, output_path):
        from pyspark import SparkContext

        sc = SparkContext(appName='dedupe venues MRJob')

        lines = sc.textFile(input_path)

        geojson_lines = lines.map(lambda line: json.loads(line.rstrip()))
        geojson_ids = geojson_lines.cache().zipWithIndex()
        id_geojson = geojson_ids.map(lambda geojson_uid: (geojson_uid[1], geojson_uid[0]))

        address_ids = geojson_ids.map(lambda geojson_uid1: (Address.from_geojson(geojson_uid1[0]), geojson_uid1[1]))

        geo_model = not self.options.no_geo_model

        dupes_only = self.options.dupes_only
        use_latlon = not self.options.no_latlon
        use_city = self.options.use_city
        use_postal_code = self.options.use_postal_code

        if not self.options.address_only:
            dupes_with_classes_and_sims = VenueDeduperSpark.dupe_sims(address_ids, geo_model=geo_model, use_latlon=use_latlon, use_city=use_city, use_postal_code=use_postal_code)
        else:
            dupes_with_classes_and_sims = AddressDeduperSpark.dupe_sims(address_ids, geo_model=geo_model, use_latlon=use_latlon, use_city=use_city, use_postal_code=use_postal_code)

        dupes = dupes_with_classes_and_sims.filter(lambda uid1_uid2_classification_sim: uid1_uid2_classification_sim[1][0] in (DedupeResponse.classifications.EXACT_DUPE, DedupeResponse.classifications.LIKELY_DUPE)) \
                                           .map(lambda uid1_uid2_classification_sim2: (uid1_uid2_classification_sim2[0][0], True)) \
                                           .distinct()

        possible_dupe_pairs = dupes_with_classes_and_sims.map(lambda uid1_uid2_classification_sim3: (uid1_uid2_classification_sim3[0][0], True)) \
                                                         .distinct()

        canonicals = dupes_with_classes_and_sims.map(lambda uid1_uid2_classification_sim4: (uid1_uid2_classification_sim4[0][1], (uid1_uid2_classification_sim4[0][0], uid1_uid2_classification_sim4[1][0], uid1_uid2_classification_sim4[1][1]))) \
                                                .subtractByKey(dupes) \
                                                .map(lambda uid2_uid1_classification_sim: (uid2_uid1_classification_sim[0], True)) \
                                                .distinct()

        dupes_with_canonical = dupes_with_classes_and_sims.map(lambda uid1_uid2_classification_sim5: (uid1_uid2_classification_sim5[0][1], (uid1_uid2_classification_sim5[0][0], uid1_uid2_classification_sim5[1][0], uid1_uid2_classification_sim5[1][1]))) \
                                                          .leftOuterJoin(canonicals) \
                                                          .map(lambda uid2_uid1_classification_sim_is_canonical: ((uid2_uid1_classification_sim_is_canonical[0][0], uid2_uid1_classification_sim_is_canonical[0]), (uid2_uid1_classification_sim_is_canonical[0][1], uid2_uid1_classification_sim_is_canonical[1][1] or False, uid2_uid1_classification_sim_is_canonical[0][2])))

        if not self.options.address_only:
            explain = DedupeResponse.explain_venue_dupe(name_dupe_threshold=self.options.name_dupe_threshold,
                                                        name_review_threshold=self.options.name_review_threshold,
                                                        with_unit=self.options.with_unit)
        else:
            explain = DedupeResponse.explain_address_dupe(with_unit=self.options.with_unit)

        dupe_responses = dupes_with_canonical.map(lambda uid1_uid2_classification_is_canonical_sim: ((uid1_uid2_classification_is_canonical_sim[0][1], (uid1_uid2_classification_is_canonical_sim[0][0], uid1_uid2_classification_is_canonical_sim[1][0], uid1_uid2_classification_is_canonical_sim[1][1], uid1_uid2_classification_is_canonical_sim[1][2])))) \
                                             .join(id_geojson) \
                                             .map(lambda uid2_uid1_classification_is_canonical_sim_val2: (uid2_uid1_classification_is_canonical_sim_val2[0][0], (uid2_uid1_classification_is_canonical_sim_val2[1][1], uid2_uid1_classification_is_canonical_sim_val2[0][1], uid2_uid1_classification_is_canonical_sim_val2[0][2], uid2_uid1_classification_is_canonical_sim_val2[0][3]))) \
                                             .groupByKey() \
                                             .leftOuterJoin(dupes) \
                                             .join(id_geojson) \
                                             .map(lambda uid1_same_as_is_dupe_value: (uid1_same_as_is_dupe_value[0], DedupeResponse.create(uid1_same_as_is_dupe_value[1][1], uid1_same_as_is_dupe_value[0][1]=uid1_same_as_is_dupe_value[0][1] or False, add_random_guid=True, uid1_same_as_is_dupe_value[0][0]=uid1_same_as_is_dupe_value[0][0], explain=explain)))

        if dupes_only:
            all_responses = list(dupe_responses.values()) \
                                          .map(lambda response: json.dumps(response))
        else:
            non_dupe_responses = id_geojson.subtractByKey(possible_dupe_pairs) \
                                           .map(lambda uid_value: (uid_value[0], DedupeResponse.base_response(uid_value[1], is_dupe=False)))

            all_responses = list(non_dupe_responses.union(dupe_responses) \
                                              .sortByKey().values()) \
                                              .map(lambda response: json.dumps(response))

        all_responses.saveAsTextFile(output_path)

        sc.stop()

if __name__ == '__main__':
    DedupeVenuesJob.run()
