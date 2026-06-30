export function buildTagTreeRequestOptions() {
  return {
    params: {
      include: 'children,lastPostedDiscussion,parent',
      include_children: true,
    },
  }
}
