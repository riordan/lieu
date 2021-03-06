

class IDPairRDD(object):
    @classmethod
    def join_pairs(cls, pairs, kvs):
        result = pairs.join(kvs) \
                      .map(lambda k1_k2_v1: (k1_k2_v1[1][0], (k1_k2_v1[0], k1_k2_v1[1][1])))

        num_partitions = result.getNumPartitions()

        return result.join(kvs) \
                     .map(lambda k2_k1_v1_v2: ((k2_k1_v1_v2[0][0], k2_k1_v1_v2[0]), (k2_k1_v1_v2[0][1], k2_k1_v1_v2[1][1]))) \
                     .coalesce(num_partitions)
