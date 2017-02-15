# ParkingLot provides an abstraction for a fair waitqueue with cancellation
# and requeueing support. Inspiration:
#
#    https://webkit.org/blog/6161/locking-in-webkit/
#    https://amanieu.github.io/parking_lot/
#
# which were in turn heavily influenced by
#
#    http://gee.cs.oswego.edu/dl/papers/aqs.pdf
#
# Compared to these, our use of cooperative scheduling allows some
# simplifications (no need for internal locking). On the other hand, the need
# to support trio's strong cancellation semantics adds some complications
# (tasks need to know where they're queued so they can cancel). Also, in the
# above work, the ParkingLot is a global structure that holds a collection of
# waitqueues keyed by lock address, and which are opportunistically allocated
# and destroyed as contention arises; this allows the worst-case memory usage
# for all waitqueues to be O(#tasks). Here we allocate a separate wait queue
# for each synchronization object, so we're O(#objects + #tasks). This isn't
# *so* bad since compared to our synchronization objects are heavier than
# theirs and our tasks are lighter, so for us #objects is smaller and #tasks
# is larger.
#
# This is in the core because for two reasons. First, it's used by
# UnboundedQueue, and UnboundedQueue is used for a number of things in the
# core. And second, it's responsible for providing fairness to all of our
# high-level synchronization primitives (locks, queues, etc.). For now with
# our FIFO scheduler this is relatively trivial (it's just a FIFO waitqueue),
# but in the future we ever start support task priorities or fair scheduling
#
#    https://github.com/njsmith/trio/issues/32
#
# then all we'll have to do is update this. (Well, full-fledged task
# priorities might also require priority inheritance, which would require more
# work.)
#
# For discussion of data structures to use here, see:
#
#     https://github.com/dabeaz/curio/issues/136
#
# (and also the articles above). Currently we use a SortedDict ordered by a
# global monotonic counter that ensures FIFO ordering. The main advantage of
# this is that it's easy to implement :-). An intrusive doubly-linked list
# would also be a natural approach, so long as we only handle FIFO ordering.
#
# XX: should we switch to the shared global ParkingLot approach?
#
# XX: we should probably add support for "parking tokens" to allow for
# task-fair RWlock (basically: when parking a task needs to be able to mark
# itself as a reader or a writer, and then a task-fair wakeup policy is, wake
# the next task, and if it's a reader than keep waking tasks so long as they
# are readers). Without this I think you can implement write-biased or
# read-biased RWlocks (by using two parking lots and drawing from whichever is
# preferred), but not task-fair -- and task-fair plays much more nicely with
# WFQ. (Consider what happens in the two-lot implementation if you're
# write-biased but all the pending writers are blocked at the scheduler level
# by the WFQ logic...)
# ...alternatively, "phase-fair" RWlocks are pretty interesting:
#    http://www.cs.unc.edu/~anderson/papers/ecrts09b.pdf
# Useful summary:
# https://docs.oracle.com/javase/7/docs/api/java/util/concurrent/locks/ReadWriteLock.html
#
# XX: if we do add WFQ, then we might have to drop the current feature where
# unpark returns the tasks that were unparked. Rationale: suppose that at the
# time we call unpark, the next task is deprioritized... and then, before it
# becomes runnable, a new task parks which *is* runnable. Ideally we should
# immediately wake the new task, and leave the old task on the queue for
# later. But this means we can't commit to which task we are unparking when
# unpark is called.
#
# XX: maybe drop the ability to specify what unpark returns? It's unused right
# now. And it might be useful to have it instead return a ticket that can be
# used to re-park without giving up the place in line?
#
# See: https://github.com/njsmith/trio/issues/53

from itertools import count
import attr
from sortedcontainers import SortedDict

from .. import _core
from . import _hazmat

__all__ = ["ParkingLot"]

_counter = count()

class _AllType:
    def __repr__(self):
        return "ParkingLot.ALL"

@attr.s(frozen=True)
class _ParkingLotStatistics:
    tasks_waiting = attr.ib()

@_hazmat
@attr.s(slots=True, cmp=False, hash=False)
class ParkingLot:
    # {idx: [task, idx, lot]}
    _parked = attr.ib(default=attr.Factory(SortedDict))

    ALL = _AllType()

    def statistics(self):
        return _ParkingLotStatistics(tasks_waiting=len(self._parked))

    def __len__(self):
        return len(self._parked)

    def __bool__(self):
        return bool(self._parked)

    def _deposit_ticket(self, ticket):
        # On entry, 'ticket' is a 3-list where the first element is the task
        # and we overwrite the other two.
        idx = next(_counter)
        ticket[1] = idx
        ticket[2] = self
        self._parked[idx] = ticket

    @_core.enable_ki_protection
    async def park(self):
        ticket = [_core.current_task(), None, None]
        self._deposit_ticket(ticket)
        def abort(_):
            task, idx, lot = ticket
            del lot._parked[idx]
            return _core.Abort.SUCCEEDED
        return await _core.yield_indefinitely(abort)

    def _pop_several(self, count):
        if count is ParkingLot.ALL:
            count = len(self._parked)
        for _ in range(min(count, len(self._parked))):
            _, ticket = self._parked.popitem(last=False)
            yield ticket

    @_core.enable_ki_protection
    def unpark(self, *, count=ALL, result=_core.Value(None)):
        tasks = [task for (task, _, _) in self._pop_several(count)]
        for task in tasks:
            _core.reschedule(task, result)
        return tasks

    @_core.enable_ki_protection
    def repark(self, new_lot, *, count=ALL):
        if not isinstance(new_lot, ParkingLot):
            raise TypeError("new_lot must be a ParkingLot")
        for ticket in self._pop_several(count):
            new_lot._deposit_ticket(ticket)
