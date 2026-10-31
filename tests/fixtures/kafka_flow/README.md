# repo-a classifier pipeline

Messages on kafka-a topic-a are classified by fn-a and queued on queue-a; fn-b stores
them in table-a. svc-a (ECS, behind alb-a) serves reads from table-a.
