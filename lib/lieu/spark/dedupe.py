from six import itertools

from lieu.address import AddressComponents, Coordinates
from lieu.api import DedupeResponse
from lieu.dedupe import AddressDeduper, NameDeduper, VenueDeduper
from lieu.similarity import soft_tfidf_similarity, jaccard_similarity
from lieu.tfidf import TFIDF

from lieu.spark.tfidf import TFIDFSpark, GeoTFIDFSpark
from lieu.spark.utils import IDPairRDD


class AddressDeduperSpark(object):
    @classmethod
    def address_dupe_pairs(cls, address_hashes, sub_building=False):
        dupe_pairs = list(address_hashes.groupByKey() \
                                   .filter(lambda key_vals: len(key_vals[1]) > 1).values()) \
                                   .flatMap(lambda vals: [(max(uid1, uid2), min(uid1, uid2))
                                                          for (uid1, a1), (uid2, a2) in itertools.combinations(vals, 2)
                                                          if AddressDeduper.is_address_dupe(a1, a2) and
                                                          (not sub_building or AddressDeduper.is_sub_building_dupe(a1, a2))]) \
                                   .distinct()

        return dupe_pairs

    @classmethod
    def dupe_sims(cls, address_ids, use_latlon=True, use_city=False, use_postal_code=False):
        address_hashes = address_ids.flatMap(lambda address_uid: [(h, (address_uid[1], address_uid[0])) for h in AddressDeduper.near_dupe_hashes(address_uid[0], use_latlon=use_latlon, use_city=use_city, use_postal_code=use_postal_code)])

        return cls.address_dupe_pairs(address_hashes) \
                  .map(lambda uid1_uid2: ((uid1_uid2[0], uid1_uid2[1]), (DedupeResponse.classifications.EXACT_DUPE, 1.0)))


class VenueDeduperSpark(object):
    DEFAULT_GEO_MODEL_PROPORTION = 0.6

    @classmethod
    def names(cls, address_ids):
        return address_ids.map(lambda address_uid1: (address_uid1[0].get(AddressComponents.NAME, ''), address_uid1[1]))

    @classmethod
    def names_geo(cls, address_ids):
        return address_ids.map(lambda address_uid2: ((address_uid2[0].get(AddressComponents.NAME, ''), address_uid2[0].get(Coordinates.LATITUDE), address_uid2[0].get(Coordinates.LONGITUDE)), address_uid2[1]) )

    @classmethod
    def batch_doc_count(cls, docs):
        return docs.count()

    @classmethod
    def name_similarity(cls, tfidf1, tfidf2):
        return soft_tfidf_similarity(TFIDF.normalized_tfidf_vector(tfidf1),
                                     TFIDF.normalized_tfidf_vector(tfidf2))

    @classmethod
    def name_geo_similarity(cls, tfidf1, tfidf2, geo_tfidf1, geo_tfidf2, geo_model_proportion=DEFAULT_GEO_MODEL_PROPORTION):
        tfidf_sim = soft_tfidf_similarity(TFIDF.normalized_tfidf_vector(tfidf1),
                                          TFIDF.normalized_tfidf_vector(tfidf2))
        geo_tfidf_sim = soft_tfidf_similarity(TFIDF.normalized_tfidf_vector(geo_tfidf1),
                                              TFIDF.normalized_tfidf_vector(geo_tfidf2))

        return (geo_model_proportion * geo_tfidf_sim) + ((1.0 - geo_model_proportion) * tfidf_sim)

    @classmethod
    def dupe_sims(cls, address_ids, geo_model=True, doc_frequency=None, geo_doc_frequency=None, total_docs=0, total_docs_by_geo=None,
                  min_name_word_count=1, min_geo_name_word_count=1, name_dupe_threshold=DedupeResponse.default_name_dupe_threshold,
                  name_review_threshold=DedupeResponse.default_name_review_threshold, geo_model_proportion=DEFAULT_GEO_MODEL_PROPORTION,
                  use_latlon=True, use_city=False, use_postal_code=False):
        name_ids = cls.names(address_ids)
        name_word_counts = TFIDFSpark.doc_word_counts(name_ids, has_id=True)
        batch_doc_frequency = TFIDFSpark.doc_frequency(name_word_counts)

        if doc_frequency is not None:
            doc_frequency = TFIDFSpark.update_doc_frequency(doc_frequency, batch_doc_frequency)
        else:
            doc_frequency = batch_doc_frequency

        name_geo_word_counts = None

        if geo_model:
            name_geo_ids = cls.names_geo(address_ids)
            batch_docs_by_geo = GeoTFIDFSpark.total_docs_by_geo(name_geo_ids, has_id=True)
            if total_docs_by_geo is None:
                total_docs_by_geo = batch_docs_by_geo
            else:
                total_docs_by_geo = GeoTFIDFSpark.update_total_docs_by_geo(total_docs_by_geo, batch_docs_by_geo)

            geo_aliases = GeoTFIDFSpark.geo_aliases(total_docs_by_geo)
            updated_total_docs_geo_aliases = GeoTFIDFSpark.updated_total_docs_geo_aliases(total_docs_by_geo, geo_aliases)

            name_geo_word_counts = GeoTFIDFSpark.doc_word_counts(name_geo_ids, has_id=True, geo_aliases=geo_aliases)

            batch_geo_doc_frequency = GeoTFIDFSpark.doc_frequency(name_geo_word_counts)

            if geo_doc_frequency is not None:
                geo_doc_frequency = GeoTFIDFSpark.update_doc_frequency(geo_doc_frequency, batch_geo_doc_frequency)
            else:
                geo_doc_frequency = batch_geo_doc_frequency

        batch_docs = cls.batch_doc_count(address_ids)

        total_docs += batch_docs
        names_tfidf = TFIDFSpark.docs_tfidf(name_word_counts, doc_frequency, total_docs)
        names_geo_tfidf = None

        if geo_model:
            names_geo_tfidf = GeoTFIDFSpark.docs_tfidf(name_geo_word_counts, geo_doc_frequency, updated_total_docs_geo_aliases)

        address_hashes = address_ids.flatMap(lambda address_uid3: [(h, (address_uid3[1], address_uid3[0])) for h in VenueDeduper.near_dupe_hashes(address_uid3[0], use_latlon=use_latlon, use_city=use_city, use_postal_code=use_postal_code)])

        address_dupe_pairs = AddressDeduperSpark.address_dupe_pairs(address_hashes)

        id_names = name_ids.map(lambda name_uid: (name_uid[1], name_uid[0]))

        exact_dupe_pairs = list(IDPairRDD.join_pairs(address_dupe_pairs, id_names) \
                                    .filter(lambda uid1_uid2_name1_name2: VenueDeduper.is_exact_name_dupe(uid1_uid2_name1_name2[1][0], uid1_uid2_name1_name2[1][1])).keys())

        if not geo_model:
            dupe_pair_sims = IDPairRDD.join_pairs(address_dupe_pairs, names_tfidf) \
                                      .mapValues(lambda tfidf1_tfidf2: cls.name_similarity(list(tfidf1_tfidf2[0].items()), list(tfidf1_tfidf2[1].items())))
        else:
            dupe_pair_sims = IDPairRDD.join_pairs(address_dupe_pairs, names_tfidf.join(names_geo_tfidf)) \
                                      .mapValues(lambda tfidf1_geo_tfidf1_tfidf2_geo_tfidf2: cls.name_geo_similarity(list(tfidf1_geo_tfidf1_tfidf2_geo_tfidf2[0][0].items()), list(tfidf1_geo_tfidf1_tfidf2_geo_tfidf2[1][0].items()),
                                                                                                                              list(tfidf1_geo_tfidf1_tfidf2_geo_tfidf2[0][1].items()), list(tfidf1_geo_tfidf1_tfidf2_geo_tfidf2[1][1].items()),
                                                                                                                              geo_model_proportion=geo_model_proportion))

        exact_dupe_sims = exact_dupe_pairs.map(lambda uid1_uid24: ((uid1_uid24[0], uid1_uid24[1]), (DedupeResponse.classifications.EXACT_DUPE, 1.0)))

        possible_dupe_sims = dupe_pair_sims.filter(lambda uid1_uid2_sim: uid1_uid2_sim[1] >= name_review_threshold) \
                                           .mapValues(lambda sim: (DedupeResponse.classifications.LIKELY_DUPE if sim >= name_dupe_threshold else DedupeResponse.classifications.NEEDS_REVIEW, sim)) \
                                           .subtractByKey(exact_dupe_sims)

        all_dupe_sims = possible_dupe_sims.union(exact_dupe_sims)

        return all_dupe_sims
