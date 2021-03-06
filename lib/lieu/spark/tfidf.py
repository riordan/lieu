import geohash
import math

from six import operator

from collections import Counter

from lieu.tfidf import TFIDF
from lieu.dedupe import NameDeduper


class TFIDFSpark(object):
    @classmethod
    def doc_word_counts(cls, docs, has_id=False):
        if not has_id:
            docs = docs.zipWithUniqueId()

        doc_word_counts = docs.flatMap(lambda doc_doc_id: [(word, (doc_doc_id[1], count))
                                                              for word, count in list(Counter(NameDeduper.content_tokens(doc_doc_id[0])).items())])
        return doc_word_counts

    @classmethod
    def doc_frequency(cls, doc_word_counts):
        doc_frequency = doc_word_counts.map(lambda word_doc_id_count: (word_doc_id_count[0], 1)).reduceByKey(lambda x, y: x + y)
        return doc_frequency

    @classmethod
    def filter_min_doc_frequency(cls, doc_frequency, min_count=2):
        return doc_frequency.filter(lambda key_count: key_count[1] >= min_count)

    @classmethod
    def update_doc_frequency(cls, doc_frequency, batch_frequency):
        updated = doc_frequency.union(batch_frequency).reduceByKey(lambda x, y: x + y)
        return updated

    @classmethod
    def docs_tfidf(cls, doc_word_counts, doc_frequency, total_docs, min_count=1):
        if min_count > 1:
            doc_frequency = cls.filter_min_doc_frequency(doc_frequency, min_count=min_count)

        num_partitions = doc_word_counts.getNumPartitions()

        doc_ids_word_stats = doc_word_counts.join(doc_frequency).map(lambda word_doc_id_term_frequency_doc_frequency: (word_doc_id_term_frequency_doc_frequency[0][0], (word_doc_id_term_frequency_doc_frequency[0], word_doc_id_term_frequency_doc_frequency[0][1], word_doc_id_term_frequency_doc_frequency[1][1])))
        docs_tfidf = doc_ids_word_stats.groupByKey() \
                                       .mapValues(lambda vals: {word: TFIDF.tfidf_score(term_frequency, doc_frequency, total_docs)
                                                                for word, term_frequency, doc_frequency in vals})
        return docs_tfidf.coalesce(num_partitions)


class GeoTFIDFSpark(TFIDFSpark):
    DEFAULT_GEOHASH_PRECISION = 4

    @classmethod
    def doc_geohash(cls, lat, lon, geohash_precision=DEFAULT_GEOHASH_PRECISION):
        return geohash.encode(lat, lon)[:geohash_precision]

    @classmethod
    def doc_word_counts(cls, docs, geo_aliases=None, has_id=False, geohash_precision=DEFAULT_GEOHASH_PRECISION):
        if not has_id:
            docs = docs.zipWithUniqueId()

        docs = docs.filter(lambda doc_lat_lon_doc_id2: doc_lat_lon_doc_id2[0][1] is not None and doc_lat_lon_doc_id2[0][2] is not None)

        if geo_aliases:
            doc_geohashes = docs.map(lambda doc_lat_lon_doc_id: (cls.doc_geohash(doc_lat_lon_doc_id[0][1], doc_lat_lon_doc_id[0][2]), (doc_lat_lon_doc_id[0][0], doc_lat_lon_doc_id[1]))) \
                                .leftOuterJoin(geo_aliases) \
                                .map(lambda geo_doc_doc_id_geo_alias: (geo_doc_doc_id_geo_alias[1][1] or geo_doc_doc_id_geo_alias[0], geo_doc_doc_id_geo_alias[0][0], geo_doc_doc_id_geo_alias[0][1]))
        else:
            doc_geohashes = docs.map(lambda doc_lat_lon_doc_id1: (cls.doc_geohash(doc_lat_lon_doc_id1[0][1], doc_lat_lon_doc_id1[0][2]), doc_lat_lon_doc_id1[0][0], doc_lat_lon_doc_id1[1]))

        doc_word_counts = doc_geohashes.flatMap(lambda geo_doc_doc_id: [((geo_doc_doc_id[0], word), (geo_doc_doc_id[2], count))
                                                                            for word, count in list(Counter(NameDeduper.content_tokens(geo_doc_doc_id[1])).items())])
        return doc_word_counts

    @classmethod
    def geo_aliases(cls, total_docs_by_geo, min_doc_count=1000):
        keep_geos = total_docs_by_geo.filter(lambda geo_count: geo_count[1] >= min_doc_count)
        alias_geos = total_docs_by_geo.subtract(keep_geos)
        return list(alias_geos.keys()) \
                         .flatMap(lambda key: [(neighbor, key) for neighbor in geohash.neighbors(key)]) \
                         .join(keep_geos) \
                         .map(lambda neighbor_key_count: (neighbor_key_count[1][0], (neighbor_key_count[0], neighbor_key_count[1][1]))) \
                         .groupByKey() \
                         .map(lambda key_values: (key_values[0], sorted(key_values[1], key_values[0]=operator.itemgetter(1), reverse=True)[0][0]))

    @classmethod
    def total_docs_by_geo(cls, docs, has_id=False, geohash_precision=DEFAULT_GEOHASH_PRECISION):
        if not has_id:
            docs = docs.zipWithUniqueId()

        docs = docs.filter(lambda doc_lat_lon_doc_id3: doc_lat_lon_doc_id3[0][1] is not None and doc_lat_lon_doc_id3[0][2] is not None)

        total_docs_by_geo = docs.map(lambda doc_lat_lon_doc_id4: (cls.doc_geohash(doc_lat_lon_doc_id4[0][1], doc_lat_lon_doc_id4[0][2]), 1)) \
                                .reduceByKey(lambda x, y: x + y)
        return total_docs_by_geo

    @classmethod
    def update_total_docs_by_geo(cls, total_docs_by_geo, batch_docs_by_geo):
        updated = total_docs_by_geo.union(batch_docs_by_geo).reduceByKey(lambda x, y: x + y)
        return updated

    @classmethod
    def updated_total_docs_geo_aliases(cls, total_docs_by_geo, geo_aliases):
        batch_docs_by_geo = total_docs_by_geo.join(geo_aliases) \
                                             .map(lambda geo_count_geo_alias: (geo_count_geo_alias[1][1], geo_count_geo_alias[1][0])) \
                                             .reduceByKey(lambda x, y: x + y)

        return cls.update_total_docs_by_geo(total_docs_by_geo, batch_docs_by_geo) \
                  .subtractByKey(geo_aliases)

    @classmethod
    def docs_tfidf(cls, doc_word_counts, geo_doc_frequency, total_docs_by_geo, min_count=1):
        if min_count > 1:
            geo_doc_frequency = cls.filter_min_doc_frequency(geo_doc_frequency, min_count=min_count)

        num_partitions = doc_word_counts.getNumPartitions()

        geo_doc_frequency_totals = geo_doc_frequency.map(lambda geo_word_count: (geo_word_count[0][0], (geo_word_count[0][1], geo_word_count[1]))) \
                                                    .join(total_docs_by_geo) \
                                                    .map(lambda geo_word_count_num_docs: ((geo_word_count_num_docs[0], geo_word_count_num_docs[0][0]), (geo_word_count_num_docs[0][1], geo_word_count_num_docs[1][1])))
        doc_ids_word_stats = doc_word_counts.join(geo_doc_frequency_totals) \
                                            .map(lambda geo_word_doc_id_term_frequency_doc_frequency_num_docs: (geo_word_doc_id_term_frequency_doc_frequency_num_docs[0][0], (geo_word_doc_id_term_frequency_doc_frequency_num_docs[0][0], geo_word_doc_id_term_frequency_doc_frequency_num_docs[0][1], geo_word_doc_id_term_frequency_doc_frequency_num_docs[0][1], geo_word_doc_id_term_frequency_doc_frequency_num_docs[1][0], geo_word_doc_id_term_frequency_doc_frequency_num_docs[1][1])))

        docs_tfidf = doc_ids_word_stats.groupByKey() \
                                       .mapValues(lambda vals: {word: TFIDF.tfidf_score(term_frequency, doc_frequency, num_docs)
                                                                for geo, word, term_frequency, doc_frequency, num_docs in vals})
        return docs_tfidf.coalesce(num_partitions)
